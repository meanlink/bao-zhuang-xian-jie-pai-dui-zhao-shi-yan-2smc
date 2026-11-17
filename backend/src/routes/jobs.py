from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from src.config.database import get_db
from src.models import Job
from src.schemas.job import JobInput
from src.middlewares.auth import current_user, can_write, admin_only
from src.services import jobs, material
from src.services.audit import record
from src.utils.serialization import job_dict
router=APIRouter(prefix='/jobs')


def _with_effective_recall(db,j,data):
    """Flag a job whose dataset is covered by a recall whose scan has not reached it yet."""
    if not data.get('recall_ticket_id'):
        ticket=material.dataset_under_recall(db,j.dataset_id)
        if ticket:
            data=data.copy();data['recall_ticket_id']=ticket;data['recall_status']='quarantined'
    return data
@router.get('')
def listing(db=Depends(get_db),u=Depends(current_user)):
    return [_with_effective_recall(db,j,job_dict(j)) for j in db.scalars(select(Job).order_by(Job.id.desc()).limit(100))]
@router.post('',status_code=201)
def create(data:JobInput,db=Depends(get_db),u=Depends(can_write)):
    j=jobs.create(db,data,u); record(db,u.email,'job.create',{'id':j.id,'params':j.params})
    db.commit(); jobs.enqueue(j.id); return job_dict(j)
@router.get('/{id}')
def detail(id:int,db=Depends(get_db),u=Depends(current_user)):
    j=db.get(Job,id)
    if not j: raise HTTPException(404,'分析任务不存在')
    return _with_effective_recall(db,j,job_dict(j))
@router.post('/{id}/approve')
def approve(id:int,db=Depends(get_db),u=Depends(admin_only)):
    material.lock_gate(db)
    j=db.get(Job,id)
    if not j: raise HTTPException(404,'分析任务不存在')
    material.guard_job(db,j)
    if j.status!='completed':raise HTTPException(409,'仅已完成结果可以批准')
    j.approved=True; record(db,u.email,'job.approve',{'id':id}); db.commit()
    return _with_effective_recall(db,j,job_dict(j))
