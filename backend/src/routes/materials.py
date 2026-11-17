from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select, func

from src.config.database import get_db
from src.middlewares.auth import current_user, can_write, admin_only
from src.models import (Roll, RollLink, SegmentUsage, Dataset, Job, Report, RecallTicket, RecallRoll,
                        RecallSegment, RecallDataset, RecallJob, RecallReport, DatasetRun)
from src.schemas.material import RollInput, RollLinkInput, UsageInput, RecallInput, ScanInput
from src.services import material
from src.services.audit import record

router = APIRouter(prefix='/materials')


def _roll_dict(r):
    return {'id': r.id, 'code': r.code, 'name': r.name, 'note': r.note, 'created_at': r.created_at,
            'recalled': bool(r.recall_ticket_id), 'ticket_id': r.recall_ticket_id}


def _link_dict(l):
    return {'id': l.id, 'parent_id': l.parent_id, 'child_id': l.child_id, 'kind': l.kind, 'created_at': l.created_at}


def _ticket_dict(t):
    return {'id': t.id, 'roll_id': t.roll_id, 'reason': t.reason, 'status': t.status, 'created_by': t.created_by,
            'created_at': t.created_at, 'started_at': t.started_at, 'finished_at': t.finished_at,
            'scanned_rolls': t.scanned_rolls, 'last_batch_at': t.last_batch_at, 'error': t.error}


@router.get('/rolls')
def rolls(db=Depends(get_db), u=Depends(current_user)):
    items = list(db.scalars(select(Roll).order_by(Roll.id)))
    links = list(db.scalars(select(RollLink)))
    counts = dict(db.execute(select(SegmentUsage.roll_id, func.count()).group_by(SegmentUsage.roll_id)).all())
    return {'rolls': [{**_roll_dict(r), 'usage_count': counts.get(r.id, 0)} for r in items],
            'links': [_link_dict(l) for l in links]}


@router.post('/rolls', status_code=201)
def add_roll(data: RollInput, db=Depends(get_db), u=Depends(can_write)):
    roll = material.create_roll(db, data.code, data.name, data.note)
    record(db, u.email, 'material.roll.create', {'id': roll.id, 'code': roll.code})
    db.commit()
    return _roll_dict(roll)


@router.post('/links', status_code=201)
def add_link(data: RollLinkInput, db=Depends(get_db), u=Depends(can_write)):
    link = material.link_rolls(db, data.parent, data.child, data.kind)
    record(db, u.email, 'material.link.create', {'id': link.id, 'parent': data.parent, 'child': data.child, 'kind': data.kind})
    db.commit()
    return _link_dict(link)


@router.post('/usages', status_code=201)
def add_usage(data: UsageInput, db=Depends(get_db), u=Depends(can_write)):
    usage = material.attach_usage(db, data.roll, data.run, data.note)
    record(db, u.email, 'material.usage.create', {'roll': data.roll, 'run': data.run})
    db.commit()
    return {'id': usage.id, 'roll_id': usage.roll_id, 'run': usage.run, 'note': usage.note}


@router.get('/rolls/{roll_id}/usages')
def roll_usages(roll_id: int, db=Depends(get_db), u=Depends(current_user)):
    roll = db.get(Roll, roll_id)
    if not roll:
        raise HTTPException(404, '卷料不存在')
    usages = list(db.scalars(select(SegmentUsage).where(SegmentUsage.roll_id == roll_id).order_by(SegmentUsage.id)))
    return {'roll': _roll_dict(roll), 'usages': [{'id': s.id, 'run': s.run, 'note': s.note} for s in usages]}


@router.get('/rolls/{roll_id}/lineage')
def lineage(roll_id: int, db=Depends(get_db), u=Depends(current_user)):
    """Ancestors, descendants, edges and segment usages for the lineage explorer."""
    roll = db.get(Roll, roll_id)
    if not roll:
        raise HTTPException(404, '卷料不存在')
    up = material.ancestors(db, roll_id)
    down = material.descendants(db, [roll_id])
    roll_ids = set(up) | set(down) | {roll_id}
    rolls = {r.id: r for r in db.scalars(select(Roll).where(Roll.id.in_(roll_ids)))}
    links = [l for l in db.scalars(select(RollLink)) if l.parent_id in roll_ids and l.child_id in roll_ids]
    usages = list(db.scalars(select(SegmentUsage).where(SegmentUsage.roll_id.in_(roll_ids)).order_by(SegmentUsage.id)))
    return {'roll': _roll_dict(roll),
            'ancestors': [{'roll': _roll_dict(rolls[i]), 'depth': d} for i, d in sorted(up.items(), key=lambda x: x[1]) if i in rolls],
            'descendants': [{'roll': _roll_dict(rolls[i]), 'depth': d, 'parent_id': p} for i, (d, p) in sorted(down.items(), key=lambda x: (x[1][0], x[0])) if i in rolls],
            'links': [_link_dict(l) for l in links],
            'usages': [{'roll_id': s.roll_id, 'run': s.run, 'note': s.note} for s in usages]}


@router.get('/recalls')
def recalls(db=Depends(get_db), u=Depends(current_user)):
    tickets = list(db.scalars(select(RecallTicket).order_by(RecallTicket.id.desc())))
    out = []
    for t in tickets:
        progress = material.ticket_progress(db, t)
        roll = db.get(Roll, t.roll_id)
        runs = [r[0] for r in db.execute(select(RecallSegment.run).where(RecallSegment.ticket_id == t.id))]
        out.append({**_ticket_dict(t), 'roll_code': roll.code if roll else '',
                    'total_rolls': progress['total_rolls'], 'processed_rolls': progress['processed_rolls'],
                    'segments': sorted(set(runs)),
                    'datasets': db.scalar(select(func.count()).select_from(RecallDataset).where(RecallDataset.ticket_id == t.id)),
                    'jobs': db.scalar(select(func.count()).select_from(RecallJob).where(RecallJob.ticket_id == t.id)),
                    'reports': db.scalar(select(func.count()).select_from(RecallReport).where(RecallReport.ticket_id == t.id))})
    return out


@router.post('/recalls', status_code=201)
def recall(data: RecallInput, response: Response, db=Depends(get_db), u=Depends(admin_only)):
    ticket, created = material.create_recall(db, data.roll, data.reason, u.email)
    if not created:
        response.status_code = 200
    if created:
        record(db, u.email, 'material.recall.create', {'id': ticket.id, 'roll': data.roll, 'reason': ticket.reason})
        db.commit()
        # Progress the bounded scan for small lineages immediately; the worker keeps paging.
        material.advance_recalls(db, batch_size=100)
        db.commit()
        return {'ticket': _ticket_dict(ticket), 'created': True}
    # A retry must not create a second ticket, but it may safely advance the existing scan.
    material.advance_recalls(db, batch_size=100)
    db.commit()
    return {'ticket': _ticket_dict(db.get(RecallTicket, ticket.id)), 'created': False}


@router.post('/recalls/{ticket_id}/scan')
def scan(ticket_id: int, data: ScanInput | None = None, db=Depends(get_db), u=Depends(admin_only)):
    ticket = db.get(RecallTicket, ticket_id)
    if not ticket:
        raise HTTPException(404, '召回单不存在')
    material.lock_gate(db)
    material.scan_ticket(db, ticket, data.batch_size if data else material.MAX_BATCH)
    db.commit()
    ticket = db.get(RecallTicket, ticket_id)
    progress = material.ticket_progress(db, ticket)
    return {**_ticket_dict(ticket), **progress}


@router.get('/recalls/{ticket_id}/impact')
def impact(ticket_id: int, db=Depends(get_db), u=Depends(current_user)):
    ticket = db.get(RecallTicket, ticket_id)
    if not ticket:
        raise HTTPException(404, '召回单不存在')
    roll_ids = [r[0] for r in db.execute(select(RecallRoll.roll_id).where(RecallRoll.ticket_id == ticket.id))]
    rolls = {r.id: r for r in db.scalars(select(Roll).where(Roll.id.in_(roll_ids)))} if roll_ids else {}
    parents = {r[0]: r[1] for r in db.execute(select(RecallRoll.roll_id, RecallRoll.parent_id).where(RecallRoll.ticket_id == ticket.id))}
    depths = {r[0]: r[1] for r in db.execute(select(RecallRoll.roll_id, RecallRoll.depth).where(RecallRoll.ticket_id == ticket.id))}
    datasets = list(db.scalars(select(Dataset).where(Dataset.id.in_(select(RecallDataset.dataset_id).where(RecallDataset.ticket_id == ticket.id)))))
    jobs = list(db.scalars(select(Job).where(Job.id.in_(select(RecallJob.job_id).where(RecallJob.ticket_id == ticket.id)))))
    reports = list(db.scalars(select(Report).where(Report.id.in_(select(RecallReport.report_id).where(RecallReport.ticket_id == ticket.id)))))
    segments = list(db.scalars(select(RecallSegment).where(RecallSegment.ticket_id == ticket.id).order_by(RecallSegment.roll_id, RecallSegment.run)))
    return {'ticket': _ticket_dict(ticket),
            'rolls': [{'roll': _roll_dict(rolls[i]), 'depth': depths.get(i, 0), 'parent_id': parents.get(i),
                       'path': _path(db, ticket.id, i)} for i in sorted(roll_ids, key=lambda x: (depths.get(x, 0), x))],
            'segments': [{'roll_id': s.roll_id, 'roll_code': rolls.get(s.roll_id).code if rolls.get(s.roll_id) else '',
                          'run': s.run} for s in segments],
            'datasets': [{'id': d.id, 'name': d.name, 'recall_status': d.recall_status} for d in datasets],
            'jobs': [{'id': j.id, 'name': j.name, 'dataset_id': j.dataset_id, 'recall_status': j.recall_status} for j in jobs],
            'reports': [{'id': r.id, 'name': r.name, 'job_id': r.job_id, 'enabled': r.enabled, 'recall_status': r.recall_status} for r in reports]}


def _path(db, ticket_id, roll_id):
    """Root-to-roll code path recorded by the scan, so the page can render impact paths."""
    chain = []
    current = roll_id
    seen = set()
    while current is not None and current not in seen:
        seen.add(current)
        row = db.execute(select(RecallRoll.roll_id, RecallRoll.parent_id, Roll.code)
                         .join(Roll, Roll.id == RecallRoll.roll_id)
                         .where(RecallRoll.ticket_id == ticket_id, RecallRoll.roll_id == current)).first()
        if not row:
            break
        chain.append(row[2])
        current = row[1]
    root = db.get(RecallTicket, ticket_id)
    if root:
        root_roll = db.get(Roll, root.roll_id)
        if root_roll and (not chain or chain[-1] != root_roll.code):
            chain.append(root_roll.code)
    return list(reversed(chain))


@router.get('/recalls/{ticket_id}/pending')
def pending(ticket_id: int, limit: int = 200, db=Depends(get_db), u=Depends(current_user)):
    """Next unprocessed rolls for a ticket; resumes from the first unprocessed item."""
    ticket = db.get(RecallTicket, ticket_id)
    if not ticket:
        raise HTTPException(404, '召回单不存在')
    limit = max(1, min(limit, 1000))
    rows = db.execute(select(RecallRoll, Roll.code).join(Roll, Roll.id == RecallRoll.roll_id)
                      .where(RecallRoll.ticket_id == ticket_id, RecallRoll.processed.is_(False))
                      .order_by(RecallRoll.depth, RecallRoll.id).limit(limit)).all()
    progress = material.ticket_progress(db, ticket)
    return {'ticket': _ticket_dict(ticket), **progress,
            'items': [{'roll_id': rr.roll_id, 'code': code, 'depth': rr.depth, 'parent_id': rr.parent_id} for rr, code in rows]}


@router.get('/status')
def status(db=Depends(get_db), u=Depends(current_user)):
    """Compact banner data: open tickets and their progress."""
    tickets = list(db.scalars(select(RecallTicket).where(RecallTicket.status.in_(('pending', 'running'))).order_by(RecallTicket.id)))
    out = []
    for t in tickets:
        progress = material.ticket_progress(db, t)
        roll = db.get(Roll, t.roll_id)
        out.append({'id': t.id, 'roll_code': roll.code if roll else '', 'status': t.status, **progress})
    return {'open': out,
            'recalled_rolls': db.scalar(select(func.count()).select_from(Roll).where(Roll.recall_ticket_id.is_not(None))),
            'recalled_datasets': db.scalar(select(func.count()).select_from(Dataset).where(Dataset.recall_ticket_id.is_not(None))),
            'recalled_jobs': db.scalar(select(func.count()).select_from(Job).where(Job.recall_ticket_id.is_not(None))),
            'disabled_reports': db.scalar(select(func.count()).select_from(Report).where(Report.recall_ticket_id.is_not(None)))}
