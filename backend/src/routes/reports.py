import os
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from src.config.database import get_db
from src.config.settings import ARTIFACT_DIR
from src.models import Report, Delivery, Job
from src.schemas.report import ReportInput
from src.middlewares.auth import current_user, admin_only
from src.services import reports, material
from src.services.audit import record
from src.utils.serialization import report_dict, delivery_dict
router=APIRouter()


def _with_effective_recall(db,r,data):
    """Flag a report whose analysis is covered by a recall still paging through."""
    if not data.get('recall_ticket_id'):
        j=db.get(Job,r.job_id)
        ticket=(j and (j.recall_ticket_id or material.dataset_under_recall(db,j.dataset_id))) if j else None
        if ticket:
            data=data.copy();data['recall_ticket_id']=ticket;data['recall_status']='quarantined'
    return data
@router.get('/reports')
def listing(db=Depends(get_db),u=Depends(current_user)):
    return [_with_effective_recall(db,r,report_dict(r)) for r in db.scalars(select(Report).order_by(Report.id.desc()))]
@router.post('/reports',status_code=201)
def create(data:ReportInput,db=Depends(get_db),u=Depends(admin_only)):
    r=reports.create(db,data); record(db,u.email,'report.schedule',{'id':r.id,'destination':r.destination}); db.commit()
    return report_dict(r)
@router.post('/reports/{id}/toggle')
def toggle(id:int,db=Depends(get_db),u=Depends(admin_only)):
    material.lock_gate(db)
    r=db.get(Report,id)
    if not r:raise HTTPException(404,'分发计划不存在')
    if not r.enabled: material.guard_report(db,r)
    r.enabled=not r.enabled;record(db,u.email,'report.toggle',{'id':id,'enabled':r.enabled});db.commit()
    return report_dict(r)
@router.get('/deliveries')
def deliveries(db=Depends(get_db),u=Depends(current_user)):
    return [delivery_dict(d) for d in db.scalars(select(Delivery).order_by(Delivery.id.desc()).limit(100))]
@router.get('/deliveries/{id}/download')
def download(id:int,format:str='csv',db=Depends(get_db),u=Depends(current_user)):
    d=db.get(Delivery,id)
    if not d:raise HTTPException(404,'归档不存在')
    material.lock_gate(db)
    report=db.get(Report,d.report_id)
    job=db.get(Job,report.job_id) if report else None
    if (report and report.recall_ticket_id) or (job and job.recall_ticket_id) or (job and material.dataset_under_recall(db,job.dataset_id)):
        raise HTTPException(409,'该归档关联召回卷料，已停止使用')
    if format not in ('csv','json'):raise HTTPException(422,'支持 CSV 或 JSON')
    filename=d.filename+('.json' if format=='json' else '')
    p=os.path.join(ARTIFACT_DIR,filename)
    if not os.path.exists(p):raise HTTPException(404,'归档文件不存在，请重新安排分发')
    return FileResponse(p,filename=filename,media_type='application/json' if format=='json' else 'text/csv; charset=utf-8')
