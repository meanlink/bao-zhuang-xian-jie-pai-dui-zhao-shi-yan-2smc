"""Material lineage, recall propagation and the consistency gate.

Design notes
------------
* Lineage is a directed graph: a child may have several parents (merged batches).
  Link creation rejects self-links and any edge that would close a cycle.
* A recall bans the recalled roll and every downstream roll. Parents/siblings of the
  recalled roll stay usable. Segments in trial datasets are reached through the run
  index (`dataset_runs`), never by scanning dataset JSON.
* Roll-level bans are applied eagerly (single recursive UPDATE), so a descendant can
  never be attached, analysed or delivered even while a large scan is still paging.
  Segment/consumer propagation (datasets, jobs, reports) runs in bounded batches and
  records progress per roll, so an interrupted scan resumes at the next unprocessed
  roll and retries never create a second ticket.
* `_gate` serialises material writes against recall propagation. On MySQL it is a
  row lock on a single gate row; on SQLite (local dev/testing) the write transaction
  provides the same serialisation.
"""
import time
from collections import deque

from fastapi import HTTPException
from sqlalchemy import select, func, text
from sqlalchemy.exc import IntegrityError

from src.models import (MaterialGate, Roll, RollLink, SegmentUsage, DatasetRun, Dataset, Job, Report,
                        RecallTicket, RecallRoll, RecallSegment, RecallDataset, RecallJob, RecallReport)

MAX_BATCH = 200
ROLL_LINK_KINDS = ('split', 'merge')
TICKET_ACTIVE = ('pending', 'running')
TICKET_SETTLED = ('pending', 'running', 'completed')


def _gate(db, write=True):
    gate = db.get(MaterialGate, 1, with_for_update=write, populate_existing=True)
    if gate is None:
        gate = MaterialGate(id=1, version=0)
        db.add(gate)
        db.flush()
    return gate


def lock_gate(db, write=True):
    return _gate(db, write=write)


# ---------------------------------------------------------------- lineage graph

def descendants(db, root_ids):
    """{roll_id: (depth, first_parent)} for every roll downstream of root_ids."""
    seen = {}
    frontier = deque((rid, 0, None) for rid in root_ids)
    while frontier:
        rid, depth, parent = frontier.popleft()
        if rid in seen:
            continue
        seen[rid] = (depth, parent)
        for child in db.scalars(select(RollLink.child_id).where(RollLink.parent_id == rid)):
            if child not in seen:
                frontier.append((child, depth + 1, rid))
    return seen


def ancestors(db, roll_id):
    seen = {}
    frontier = deque([(roll_id, 0)])
    while frontier:
        rid, depth = frontier.popleft()
        if rid in seen:
            continue
        seen[rid] = depth
        for parent in db.scalars(select(RollLink.parent_id).where(RollLink.child_id == rid)):
            if parent not in seen:
                frontier.append((parent, depth + 1))
    return seen


def create_roll(db, code, name='', note=''):
    code = (code or '').strip()
    if len(code) < 2:
        raise HTTPException(422, '卷料编号至少需要两个字符')
    _gate(db)
    if db.scalar(select(Roll).where(Roll.code == code)):
        raise HTTPException(409, '卷料编号已存在，请使用唯一编号')
    roll = Roll(code=code, name=(name or '').strip() or code, note=(note or '').strip()[:500], created_at=time.time())
    db.add(roll)
    db.flush()
    return roll


def link_rolls(db, parent_code, child_code, kind='split'):
    """Register a split/merge edge; reject self-links, cycles and recalled branches."""
    _invalidate_dataset_cache(db)
    kind = (kind or 'split').strip()
    if kind not in ROLL_LINK_KINDS:
        raise HTTPException(422, '关联类型只支持 split 或 merge')
    _gate(db)
    parent = _resolve(db, parent_code)
    child = _resolve(db, child_code)
    if parent.id == child.id:
        raise HTTPException(422, '不能把卷料关联到自身')
    if child.id in ancestors(db, parent.id):
        raise HTTPException(409, '该关联会形成循环谱系，已拒绝')
    if covering_ticket(db, parent.id):
        raise HTTPException(409, f'来源卷 {parent.code} 已召回，不能挂接新派生卷')
    if covering_ticket(db, child.id):
        raise HTTPException(409, f'派生卷 {child.code} 已召回，不能新增来源')
    if db.scalar(select(RollLink.id).where(RollLink.parent_id == parent.id, RollLink.child_id == child.id)):
        raise HTTPException(409, '来源卷与派生卷的关联已存在')
    link = RollLink(parent_id=parent.id, child_id=child.id, kind=kind, created_at=time.time())
    db.add(link)
    db.flush()
    # The checks above run under the gate, so a recall committed just before this call is
    # visible; a recall committing after this call discovers the edge in its own scan.
    return link


def attach_usage(db, roll_code, run, note=''):
    """Register a trial segment that consumed material from a roll."""
    _invalidate_dataset_cache(db)
    run = (run or '').strip()
    if not run:
        raise HTTPException(422, '必须填写试验片段号')
    _gate(db)
    roll = _resolve(db, roll_code)
    if covering_ticket(db, roll.id):
        raise HTTPException(409, f'卷料 {roll.code} 已召回，不能挂接试验片段')
    if db.scalar(select(SegmentUsage.id).where(SegmentUsage.roll_id == roll.id, SegmentUsage.run == run)):
        raise HTTPException(409, '该卷料已登记此片段用料')
    usage = SegmentUsage(roll_id=roll.id, run=run, note=(note or '').strip()[:300], created_at=time.time())
    db.add(usage)
    db.flush()
    # A scan that already passed this roll is caught by the final sweep (see scan_ticket);
    # a scan that is behind this roll will reach it in a later batch.
    return usage


def _resolve(db, code):
    code = (code or '').strip()
    roll = db.scalar(select(Roll).where(Roll.code == code))
    if not roll:
        raise HTTPException(404, f'卷料 {code} 不存在')
    return roll


def covering_ticket(db, roll_id):
    """Ticket covering this roll directly or through any ancestor."""
    ticket_id = db.scalar(select(Roll.recall_ticket_id).where(Roll.id == roll_id, Roll.recall_ticket_id.is_not(None)))
    if ticket_id:
        return db.get(RecallTicket, ticket_id)
    for parent_id in ancestors(db, roll_id):
        ticket_id = db.scalar(select(Roll.recall_ticket_id).where(Roll.id == parent_id, Roll.recall_ticket_id.is_not(None)))
        if ticket_id:
            return db.get(RecallTicket, ticket_id)
    return None


# ---------------------------------------------------------------- recall engine

def create_recall(db, roll_code, reason, actor):
    """Create one ticket per roll. Retries, and recalls of an already-covered roll,
    return the existing ticket instead of creating a second one."""
    _gate(db)
    roll = _resolve(db, roll_code)
    existing = _ticket_for_roll(db, roll.id)
    if existing:
        return existing, False
    ticket = RecallTicket(roll_id=roll.id, reason=(reason or '').strip()[:500], status='pending',
                          created_by=actor, created_at=time.time())
    db.add(ticket)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return _ticket_for_roll(db, roll.id), False
    _register_roll(db, ticket, roll.id, 0, None)
    # Ban the whole known subtree immediately with one recursive statement. Segment-level
    # propagation for the rest of the lineage is paged by scan_ticket.
    if not _ban_subtree(db, ticket, roll.id):
        for descendant in descendants(db, [roll.id]):
            roll_row = db.get(Roll, descendant)
            if roll_row and not roll_row.recall_ticket_id:
                roll_row.recall_ticket_id = ticket.id
    db.flush()
    ban_known(db, ticket)
    return ticket, True


def _ban_subtree(db, ticket, root_id):
    """Recursive roll-level ban of the whole subtree in one statement; False if unsupported."""
    sql = text(
        'WITH RECURSIVE subtree(id) AS ('
        ' SELECT :root '
        ' UNION SELECT l.child_id FROM roll_links l JOIN subtree s ON l.parent_id = s.id'
        ') UPDATE rolls SET recall_ticket_id = :ticket '
        'WHERE id IN (SELECT id FROM subtree) AND recall_ticket_id IS NULL')
    try:
        with db.begin_nested():
            db.execute(sql, {'root': root_id, 'ticket': ticket.id})
        # The ORM may still hold stale objects from the lineage walk; refresh them.
        db.expire_all()
        return True
    except Exception:
        return False


def _ticket_for_roll(db, roll_id):
    ticket_id = db.scalar(select(RecallTicket.id).where(RecallTicket.roll_id == roll_id))
    if ticket_id:
        return db.get(RecallTicket, ticket_id)
    covering = db.scalar(select(Roll.recall_ticket_id).where(Roll.id == roll_id))
    if covering:
        return db.get(RecallTicket, covering)
    ticket_id = db.scalar(select(RecallRoll.ticket_id).join(RecallTicket, RecallTicket.id == RecallRoll.ticket_id)
                          .where(RecallRoll.roll_id == roll_id, RecallTicket.status.in_(TICKET_SETTLED)).limit(1))
    return db.get(RecallTicket, ticket_id) if ticket_id else None


def _register_roll(db, ticket, roll_id, depth, parent_id):
    if db.scalar(select(RecallRoll.id).where(RecallRoll.ticket_id == ticket.id, RecallRoll.roll_id == roll_id)):
        return False
    db.add(RecallRoll(ticket_id=ticket.id, roll_id=roll_id, depth=depth, parent_id=parent_id,
                      processed=False, discovered_at=time.time()))
    roll = db.get(Roll, roll_id)
    if roll and not roll.recall_ticket_id:
        roll.recall_ticket_id = ticket.id
    db.flush()
    return True


def _invalidate_dataset_cache(db):
    db.info.pop('recall_dataset_cache', None)


def _register_segment(db, ticket, roll, run):
    if db.scalar(select(RecallSegment.id).where(RecallSegment.ticket_id == ticket.id, RecallSegment.roll_id == roll.id,
                                                RecallSegment.run == run)):
        return
    db.add(RecallSegment(ticket_id=ticket.id, roll_id=roll.id, run=run, created_at=time.time()))
    db.flush()
    _ban_segment_consumers(db, ticket, run)


def _ban_segment_consumers(db, ticket, run):
    """Quarantine every dataset / job / report that consumed a recalled segment."""
    now = time.time()
    dataset_ids = [r[0] for r in db.execute(select(DatasetRun.dataset_id).where(DatasetRun.run == run).distinct())]
    for dataset_id in dataset_ids:
        _quarantine_dataset(db, ticket, dataset_id, now)
        for job in db.scalars(select(Job).where(Job.dataset_id == dataset_id)):
            _quarantine_job(db, ticket, job, now)


def _quarantine_dataset(db, ticket, dataset_id, now):
    _invalidate_dataset_cache(db)
    d = db.get(Dataset, dataset_id)
    if not d:
        return False
    runs = db.scalar(select(func.count()).select_from(DatasetRun).join(
        RecallSegment, RecallSegment.run == DatasetRun.run).where(
        DatasetRun.dataset_id == dataset_id, RecallSegment.ticket_id == ticket.id)) or 0
    already = bool(d.recall_ticket_id)
    if not already:
        d.recall_ticket_id = ticket.id
        d.recall_status = 'quarantined'
    # Keep a per-ticket link even when another ticket already quarantined the batch,
    # so each recall's impact page lists everything it covered.
    if not db.scalar(select(RecallDataset.id).where(RecallDataset.ticket_id == ticket.id, RecallDataset.dataset_id == dataset_id)):
        db.add(RecallDataset(ticket_id=ticket.id, dataset_id=dataset_id, runs=runs, at=now))
    db.flush()
    return not already


def _quarantine_job(db, ticket, job, now):
    already = bool(job.recall_ticket_id)
    if not already:
        job.recall_ticket_id = ticket.id
        job.recall_status = 'quarantined'
    job.approved = False
    if not db.scalar(select(RecallJob.id).where(RecallJob.ticket_id == ticket.id, RecallJob.job_id == job.id)):
        db.add(RecallJob(ticket_id=ticket.id, job_id=job.id, at=now))
    db.flush()
    _ban_job_reports(db, ticket, job, now)
    return not already


def _ban_job_reports(db, ticket, job, now):
    for report in db.scalars(select(Report).where(Report.job_id == job.id)):
        if not report.recall_ticket_id:
            report.recall_ticket_id = ticket.id
            report.recall_status = 'quarantined'
        report.enabled = False
        if not db.scalar(select(RecallReport.id).where(RecallReport.ticket_id == ticket.id, RecallReport.report_id == report.id)):
            db.add(RecallReport(ticket_id=ticket.id, report_id=report.id, at=now))
        db.flush()


def ban_known(db, ticket):
    """Unconditional ban for every roll and segment discovered so far."""
    for rr in db.scalars(select(RecallRoll).where(RecallRoll.ticket_id == ticket.id)):
        roll = db.get(Roll, rr.roll_id)
        if roll and not roll.recall_ticket_id:
            roll.recall_ticket_id = ticket.id
    for roll_id, run in db.execute(select(RecallSegment.roll_id, RecallSegment.run).where(RecallSegment.ticket_id == ticket.id)):
        _ban_segment_consumers(db, ticket, run)
    db.flush()


def scan_ticket(db, ticket, batch_size=MAX_BATCH):
    """Advance one ticket by at most batch_size rolls, across newly discovered levels.

    A roll is marked processed only after its children and segments are registered, so an
    interrupted call leaves the remaining rolls unprocessed for the next resume.
    """
    ticket = db.get(RecallTicket, ticket.id)
    if ticket.status == 'completed':
        return ticket
    if ticket.status == 'pending':
        ticket.status = 'running'
        ticket.started_at = ticket.started_at or time.time()
        db.flush()
    processed = 0
    while processed < max(1, batch_size):
        batch = list(db.scalars(select(RecallRoll).where(
            RecallRoll.ticket_id == ticket.id, RecallRoll.processed == False)
            .order_by(RecallRoll.depth, RecallRoll.id).limit(max(1, batch_size - processed))))
        if not batch:
            break
        for rr in batch:
            roll = db.get(Roll, rr.roll_id)
            if roll and not roll.recall_ticket_id:
                roll.recall_ticket_id = ticket.id
            for child_id in db.scalars(select(RollLink.child_id).where(RollLink.parent_id == rr.roll_id)):
                _register_roll(db, ticket, child_id, rr.depth + 1, rr.roll_id)
            if roll:
                for run in db.scalars(select(SegmentUsage.run).where(SegmentUsage.roll_id == rr.roll_id)):
                    _register_segment(db, ticket, roll, run)
            rr.processed = True
            processed += 1
            ticket.scanned_rolls = (ticket.scanned_rolls or 0) + 1
            ticket.last_batch_at = time.time()
            db.flush()
    _sweep_unregistered_segments(db, ticket)
    remaining = db.scalar(select(func.count()).select_from(RecallRoll).where(
        RecallRoll.ticket_id == ticket.id, RecallRoll.processed == False)) or 0
    if remaining == 0:
        # Close the window where a link or usage landed while the last roll was processed.
        for _ in range(8):
            new_links = _sweep_unregistered_links(db, ticket)
            _sweep_unregistered_segments(db, ticket)
            if not db.scalar(select(RecallRoll.id).where(RecallRoll.ticket_id == ticket.id,
                                                         RecallRoll.processed == False).limit(1)):
                if not new_links:
                    break
    if not db.scalar(select(RecallRoll.id).where(RecallRoll.ticket_id == ticket.id, RecallRoll.processed == False).limit(1)):
        ticket.status = 'completed'
        ticket.finished_at = time.time()
    db.flush()
    ban_known(db, ticket)
    return ticket


def _sweep_unregistered_links(db, ticket):
    """Register children linked from an already-covered roll during the scan."""
    covered = select(RecallRoll.roll_id).where(RecallRoll.ticket_id == ticket.id)
    rows = db.execute(select(RollLink.parent_id, RollLink.child_id, RecallRoll.depth)
                      .join(RecallRoll, RecallRoll.roll_id == RollLink.parent_id)
                      .where(RecallRoll.ticket_id == ticket.id)
                      .where(RollLink.child_id.not_in(covered))).all()
    for parent_id, child_id, depth in rows:
        _register_roll(db, ticket, child_id, depth + 1, parent_id)
        child = db.get(Roll, child_id)
        if child:
            for run in db.scalars(select(SegmentUsage.run).where(SegmentUsage.roll_id == child.id)):
                _register_segment(db, ticket, child, run)
    return len(rows)


def _sweep_unregistered_segments(db, ticket):
    """Catch usages attached to an already-scanned roll while the scan was running."""
    rows = db.execute(select(SegmentUsage.roll_id, SegmentUsage.run)
                      .join(RecallRoll, RecallRoll.roll_id == SegmentUsage.roll_id)
                      .where(RecallRoll.ticket_id == ticket.id)
                      .outerjoin(RecallSegment, (RecallSegment.ticket_id == ticket.id) &
                                 (RecallSegment.roll_id == SegmentUsage.roll_id) &
                                 (RecallSegment.run == SegmentUsage.run))
                      .where(RecallSegment.id.is_(None))).all()
    for roll_id, run in rows:
        _register_segment(db, ticket, db.get(Roll, roll_id), run)


def open_tickets(db):
    return list(db.scalars(select(RecallTicket).where(RecallTicket.status.in_(TICKET_ACTIVE)).order_by(RecallTicket.id)))


def advance_recalls(db, batch_size=MAX_BATCH):
    """Advance every open ticket one batch; each ticket runs in its own savepoint."""
    _gate(db)
    tickets = list(db.scalars(select(RecallTicket).where(RecallTicket.status.in_(TICKET_ACTIVE)).order_by(RecallTicket.id)))
    for ticket in tickets:
        ticket_id = ticket.id
        try:
            with db.begin_nested():
                scan_ticket(db, ticket, batch_size)
        except Exception as exc:
            current = db.get(RecallTicket, ticket_id, populate_existing=True)
            if current and current.status != 'completed':
                current.error = str(exc)[:990]
                current.status = 'running'
                db.flush()
    return tickets


def guard_result_publish(db, job):
    """Worker-side final check before a computed result becomes visible."""
    db.refresh(job)
    if job.recall_ticket_id:
        return False
    if job.dataset_id and dataset_under_recall(db, job.dataset_id):
        return False
    return True


# ---------------------------------------------------------------- guards

def guard_roll(db, roll_id=None, roll_code=None):
    roll = db.get(Roll, roll_id) if roll_id else _resolve(db, roll_code)
    if roll is None:
        raise HTTPException(404, '卷料不存在')
    if covering_ticket(db, roll.id):
        raise HTTPException(409, f'卷料 {roll.code} 已召回，不可继续用于分析')
    return roll


def recalled_runs(db, runs):
    """Which of the given run ids already consume recalled material (chunked query)."""
    runs = list({str(r).strip() for r in runs if str(r).strip()})
    found = set()
    for chunk_start in range(0, len(runs), 200):
        chunk = runs[chunk_start:chunk_start + 200]
        found |= {r[0] for r in db.execute(
            select(DatasetRun.run).join(RecallSegment, RecallSegment.run == DatasetRun.run)
            .where(DatasetRun.run.in_(chunk)))}
        found |= {r[0] for r in db.execute(
            select(SegmentUsage.run).join(Roll, Roll.id == SegmentUsage.roll_id)
            .where(SegmentUsage.run.in_(chunk), Roll.recall_ticket_id.is_not(None)))}
    return sorted(found)


def dataset_rolls(db, dataset_id):
    return {r[0] for r in db.execute(
        select(SegmentUsage.roll_id).join(DatasetRun, DatasetRun.run == SegmentUsage.run)
        .where(DatasetRun.dataset_id == dataset_id).distinct())}


def dataset_under_recall(db, dataset_id):
    """Ticket id when the dataset uses material from a recalled roll or its descendants.

    Every roll after a recalled one in the split/merge chain is tainted; parents and
    siblings are not, so the walk goes upward from each used roll looking for a ban.
    Memoised per session so listing pages issue one query set per dataset.
    """
    cache = db.info.setdefault('recall_dataset_cache', {})
    if dataset_id in cache:
        return cache[dataset_id]
    ticket = _dataset_under_recall_uncached(db, dataset_id)
    cache[dataset_id] = ticket
    return ticket


def _dataset_under_recall_uncached(db, dataset_id):
    roll_ids = dataset_rolls(db, dataset_id)
    if roll_ids:
        # A directly banned roll covers the dataset.
        ticket = db.scalar(select(Roll.recall_ticket_id).where(
            Roll.id.in_(roll_ids), Roll.recall_ticket_id.is_not(None)).limit(1))
        if ticket:
            return ticket
        # Otherwise walk upward: a used roll derived from a recalled roll is tainted.
        frontier, seen = set(roll_ids), set(roll_ids)
        while frontier:
            rows = db.execute(select(RollLink.parent_id, Roll.recall_ticket_id)
                              .join(Roll, Roll.id == RollLink.parent_id)
                              .where(RollLink.child_id.in_(list(frontier)))).all()
            frontier = set()
            for parent_id, recall in rows:
                if recall:
                    return recall
                if parent_id not in seen:
                    seen.add(parent_id)
                    frontier.add(parent_id)
        # No roll-level link covers it: a recall segment registered for one of its rolls does.
        covered = db.scalar(
            select(RecallSegment.ticket_id)
            .join(SegmentUsage, SegmentUsage.run == RecallSegment.run)
            .join(DatasetRun, DatasetRun.run == SegmentUsage.run)
            .where(DatasetRun.dataset_id == dataset_id,
                   SegmentUsage.roll_id == RecallSegment.roll_id).limit(1))
        if covered:
            return covered
    return db.scalar(
        select(RecallSegment.ticket_id).join(DatasetRun, DatasetRun.run == RecallSegment.run)
        .where(DatasetRun.dataset_id == dataset_id).limit(1))


def guard_dataset(db, dataset_id):
    d = db.get(Dataset, dataset_id)
    if not d:
        raise HTTPException(404, '数据批次不存在')
    if d.recall_ticket_id:
        raise HTTPException(409, '该数据批次含被召回卷料的试验片段，不可用于新分析')
    if dataset_under_recall(db, dataset_id):
        raise HTTPException(409, '该数据批次含被召回卷料的试验片段，不可用于新分析')
    return d


def guard_job(db, job):
    if not job:
        raise HTTPException(404, '分析任务不存在')
    if job.recall_ticket_id:
        raise HTTPException(409, '该分析已因卷料召回封禁，不可再审批或分发')
    if job.dataset_id and dataset_under_recall(db, job.dataset_id):
        raise HTTPException(409, '该分析使用的批次含被召回片段，已不可再使用')
    return job


def guard_report(db, report):
    if not report:
        raise HTTPException(404, '分发计划不存在')
    if report.recall_ticket_id:
        raise HTTPException(409, '该分发计划关联召回卷料，已停用')
    guard_job(db, db.get(Job, report.job_id))
    return report


def index_dataset_runs(db, dataset):
    """Maintain the run index used by recall guards (idempotent)."""
    existing = {r[0] for r in db.execute(select(DatasetRun.run).where(DatasetRun.dataset_id == dataset.id))}
    seen = set()
    for row in dataset.records or []:
        run = str(row.get('run', '')).strip()
        if not run or run in seen or run in existing:
            continue
        seen.add(run)
        db.add(DatasetRun(dataset_id=dataset.id, run=run, shift=str(row.get('shift', ''))))
    db.flush()


def ticket_progress(db, ticket):
    total = db.scalar(select(func.count()).select_from(RecallRoll).where(RecallRoll.ticket_id == ticket.id)) or 0
    done = db.scalar(select(func.count()).select_from(RecallRoll).where(
        RecallRoll.ticket_id == ticket.id, RecallRoll.processed == True)) or 0
    return {'total_rolls': total, 'processed_rolls': done}
