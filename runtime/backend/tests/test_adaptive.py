import json
import time
from concurrent.futures import ThreadPoolExecutor
from swarm.adaptive import assess, plan_assessment
from swarm.store import Store

def response(tokens=100):
    return {'content':'saved','usage':{'total_tokens':tokens}}

def plan(store,task,count=4):
    item=store.claim('engine',20)
    parts=[{'id':str(i),'title':f'Part {i}','prompt':f'Calculate {i}*7','dependencies':[]} for i in range(count)]
    store.checkpoint_response(item,response())
    assert store.complete(item,response(),plan=parts)
    return parts

def test_complexity_changes_initial_allocation_and_preserves_ceilings():
    simple=assess('Calculate 4*5')
    complex_=assess('Research and compare sources, build production architecture, implementation plan and comprehensive report')
    assert simple['initial_token_allocation']<complex_['initial_token_allocation']
    assert assess('Build a comprehensive production report',2,12000)['initial_token_allocation']==12000
    assert complex_['recommended_agents']<=complex_['agent_ceiling']

def test_auto_scales_to_ready_work_then_down_to_verifier_and_grows_tokens():
    store=Store(':memory:')
    task=store.create_task('owner','Calculate a result',8,3,500000,allocation_mode='auto')
    assert task['agent_limit']==1 and task['token_budget']==40000
    plan(store,task)
    with ThreadPoolExecutor(max_workers=8) as pool:
        workers=list(pool.map(lambda _:store.claim('engine',20),range(8)))
    workers=[w for w in workers if w]
    assert len(workers)==4
    task=store.get_task(task['id'])
    assert task['agent_limit']==4 and 40000<task['token_budget']<=500000
    assert task['resources']['assessment']['layer_width']==4
    for worker in workers:
        store.checkpoint_response(worker,response())
        store.complete(worker,response())
    verifier=store.claim('engine',20)
    assert verifier['role']=='verifier'
    assert store.get_task(task['id'])['agent_limit']==1
    assert any(e['kind']=='resource_scale' for e in store.detail(task['id'])['events'])

def test_failed_receipt_persistence_holds_funds_before_retry():
    store=Store(':memory:')
    task=store.create_task('owner','Task',1,3,25000)
    item=store.claim('engine')
    store.fail(item,'Injected receipt-storage fault',delay=0)
    assert store.get_task(task['id'])['tokens_uncertain']==20000
    retried=store.claim('engine')
    assert retried['reserved_tokens']==5000

def test_paid_publication_retry_needs_no_new_allowance_or_attempt():
    store=Store(':memory:');task=store.create_task('owner','Task',1,1,10000)
    item=store.claim('engine');store.checkpoint_response(item,response(10000))
    assert store.retry_publication(item)
    with store.tx() as db:db.execute('UPDATE items SET available_at=0')
    retry=store.claim('engine')
    assert retry['response'] and retry['attempt']==1 and retry['reserved_tokens']==0

def test_contention_does_not_refund_paid_attempts():
    store=Store(':memory:');task=store.create_task('owner','Task',2,1,50000)
    plan(store,task,2);first=store.claim('engine');second=store.claim('engine')
    store.checkpoint_response(second,{**response(1000),'content':''})
    assert store.defer_budget_contention(second)
    assert store.get_task(task['id'])['status']=='blocked'
    assert store.get_task(task['id'])['tokens_used']==1100

def test_partial_uncertain_receipt_can_settle_definitively():
    store=Store(':memory:');task=store.create_task('owner','Task',1,3,10000)
    item=store.claim('engine');store.control(task['id'],'cancel','owner')
    store.checkpoint_response(item,{**response(1000),'usage_uncertain':True})
    assert store.get_task(task['id'])['tokens_uncertain']==9000
    store.checkpoint_response(item,response(3000));store.checkpoint_response(item,response(3000))
    final=store.get_task(task['id'])
    assert final['tokens_used']==3000 and final['tokens_uncertain']==0

def test_tiny_reservation_waits_for_sibling_settlement():
    store=Store(':memory:');task=store.create_task('owner','Task',2,3,10000,allocation_mode='auto')
    plan(store,task,2);first=store.claim('engine')
    assert first and store.claim('engine') is None
    store.checkpoint_response(first,response());store.complete(first,response())
    assert store.claim('engine') is not None
    assert store.get_task(task['id'])['status']=='running'

def test_manual_limits_stay_fixed_and_auto_does_not_cross_ceiling():
    for mode in ['manual','auto']:
        store=Store(':memory:')
        task=store.create_task('owner','Task',2,3,50000,allocation_mode=mode)
        plan(store,task,4)
        a,b=store.claim('engine',20),store.claim('engine',20)
        assert a and b and store.claim('engine',20) is None
        store.extend_reservation(a,90000)
        t=store.get_task(task['id'])
        assert t['token_budget']<=50000 and t['agent_limit']<=2
        assert t['tokens_used']+t['tokens_uncertain']+t['tokens_reserved']<=50000

def test_creation_idempotency_survives_automatic_changes():
    store=Store(':memory:')
    args=('owner','Same task',8,3,500000,'request')
    task=store.create_task(*args,allocation_mode='auto')
    plan(store,task)
    store.claim('engine',20)
    assert store.create_task(*args,allocation_mode='auto')['id']==task['id']

def test_checkpoint_settles_reservation_without_double_charging():
    store=Store(':memory:')
    task=store.create_task('owner','Task',2,3,50000)
    item=store.claim('engine')
    assert store.get_task(task['id'])['tokens_reserved']>0
    store.checkpoint_response(item,response(15000))
    t=store.get_task(task['id'])
    assert t['tokens_used']==15000 and t['tokens_reserved']==0

def test_late_receipt_reconciles_recovery_or_cancellation_exactly_once():
    for action in ['recover','cancel']:
        store=Store(':memory:')
        task=store.create_task('owner','Task',1,3,10000)
        item=store.claim('engine',lease_seconds=-1 if action=='recover' else 120)
        if action=='recover':store.recover_expired()
        else:store.control(task['id'],'cancel','owner')
        assert store.get_task(task['id'])['tokens_uncertain']==10000
        store.checkpoint_response(item,response(3000))
        store.checkpoint_response(item,response(3000))
        task=store.get_task(task['id'])
        assert task['tokens_used']==3000 and task['tokens_uncertain']==0

def test_resources_survive_restart_and_low_limit_drains_without_cancellation(tmp_path):
    path=tmp_path/'nexus.db'
    store=Store(path)
    task=store.create_task('owner','Task',8,3,500000,allocation_mode='auto')
    plan(store,task)
    workers=[store.claim('engine',20) for _ in range(4)]
    configured=store.configure_resources(task['id'],'manual',1,500000,'owner')
    assert configured['active_agents']==4
    assert store.claim('engine',20) is None
    store.close()
    resumed=Store(path)
    assert resumed.get_task(task['id'])['resources']['mode']=='manual'
    assert resumed.get_task(task['id'])['agent_limit']==1
    assert resumed.configure_resources(task['id'],'auto',2,500000,'someone-else') is None
