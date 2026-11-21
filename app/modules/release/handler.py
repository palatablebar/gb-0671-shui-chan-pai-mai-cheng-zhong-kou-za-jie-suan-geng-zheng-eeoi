from fastapi import APIRouter, Request, Body
from app.engine import evaluate
from app.web import render
router=APIRouter()
@router.get('/release')
def page(request:Request):
    return render(request,'release')
@router.post('/api/release')
def calculate(payload:dict=Body(default={})):
    return evaluate(payload,until=7)
