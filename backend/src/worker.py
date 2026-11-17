import time, json, logging
from pathlib import Path
from sqlalchemy import select
from redis import Redis
from src.config.database import Session
from src.config.settings import REDIS_URL
from src.models import Job, Dataset, Report, RecallTicket
from src.services.business import analyze
from src.services import material
from src.services.reports import deliver, deliverable
from src.services.audit import record
logging.basicConfig(level=logging.INFO)
def execute(job_id):
    with Session() as db:
        j=db.get(Job,job_id)
        if not j or j.status!='queued':return
        material.lock_gate(db)
        if material.dataset_under_recall(db, j.dataset_id):
            j.recall_status='quarantined'
            j.status='failed';j.error='数据批次含召回卷料，任务已封禁';j.finished_at=time.time()
            record(db,'worker','job.recalled',{'id':j.id});db.commit();return
        j.status='running'; db.commit()
        try:
            d=db.get(Dataset,j.dataset_id)
            result=analyze(d.records,j.params)
            json.dumps(result,allow_nan=False)
            material.lock_gate(db)
            if not material.guard_result_publish(db,j):
                j.result={};j.status='failed';j.error='计算期间卷料被召回，结果不予放行'
                j.recall_status='quarantined';j.finished_at=time.time()
                record(db,'worker','job.recalled',{'id':j.id})
                db.commit();return
            j.result=result;j.status='completed';j.finished_at=time.time()
            record(db,'worker','job.completed',{'id':j.id})
        except Exception as e:
            logging.exception('Analysis failed')
            j.status='failed';j.error=str(e)[:990];j.finished_at=time.time()
            record(db,'worker','job.failed',{'id':j.id,'error':j.error})
        db.commit()
def tick():
    with Session() as db:
        queued=list(db.scalars(select(Job.id).where(Job.status=='queued').limit(3)))
        due=list(db.scalars(select(Report.id).where(Report.enabled==True,Report.next_run<=time.time()).limit(20)))
    for job_id in queued:execute(job_id)
    for report_id in due:
        # One transaction per delivery: the material gate is held only across that check+write.
        with Session() as db:
            r=db.get(Report,report_id)
            if not r or not r.enabled:continue
            d=deliverable(db,r)
            if d:record(db,'worker','report.delivered',{'id':r.id,'delivery':d.id})
            db.commit()


def recall_tick():
    """Page through open recall tickets one committed batch at a time.

    Committing per ticket keeps the material gate free between batches, so attachments,
    analyses and deliveries are never stalled behind a long lineage scan.
    """
    with Session() as db:
        open_ids=[t.id for t in material.open_tickets(db)]
    for ticket_id in open_ids:
        with Session() as db:
            ticket=db.get(RecallTicket,ticket_id)
            if not ticket or ticket.status not in material.TICKET_ACTIVE:
                continue
            before=(ticket.status,ticket.scanned_rolls)
            try:
                material.lock_gate(db)
                material.scan_ticket(db,ticket,material.MAX_BATCH)
                if before!=(ticket.status,ticket.scanned_rolls):
                    record(db,'worker','material.recall.scan',{'id':ticket.id,'status':ticket.status,'scanned':ticket.scanned_rolls})
                db.commit()
            except Exception:
                db.rollback()
                logging.exception('Recall scan retry')
                ticket=db.get(RecallTicket,ticket_id)
                if ticket:
                    ticket.error='扫描批次失败，保留已处理卷料，稍后重试'[:990]
                    db.commit()
def main():
    r=Redis.from_url(REDIS_URL,socket_timeout=3)
    # This Compose stack has a single worker. A crash must not strand its jobs.
    with Session() as db:
        for j in db.scalars(select(Job).where(Job.status=='running')):
            j.status='queued';record(db,'worker','job.recovered',{'id':j.id})
        db.commit()
    while True:
        Path('/tmp/heartbeat').touch()
        try:
            item=r.blpop('analysis:queue',timeout=1)
            if item:execute(int(item[1]))
            tick()
            recall_tick()
        except Exception:
            logging.exception('Worker retry');time.sleep(2)
if __name__=='__main__':main()
