from run_pipeline import evaluate_collection_health

def test_collection_quality_gate():
    # Case A: 6 success, >0 outputs -> Healthy
    tasks_a = [
        {"agent_name": f"agent_{i}", "status": "success", "outputs_count": 5}
        for i in range(6)
    ]
    is_healthy, count = evaluate_collection_health(tasks_a, min_required_agents=6)
    assert is_healthy is True
    assert count == 6

    # Case B: 3 success, 3 partial w/ usable evidence -> Healthy
    tasks_b = [
        {"agent_name": f"agent_{i}", "status": "success", "outputs_count": 5}
        for i in range(3)
    ] + [
        {"agent_name": f"agent_{i}", "status": "partial_success", "outputs_count": 2}
        for i in range(3, 6)
    ]
    is_healthy, count = evaluate_collection_health(tasks_b, min_required_agents=6)
    assert is_healthy is True
    assert count == 6

    # Case C: 11 partial, 0 evidence -> Rejected
    tasks_c = [
        {"agent_name": f"agent_{i}", "status": "partial_success", "outputs_count": 0}
        for i in range(11)
    ]
    is_healthy, count = evaluate_collection_health(tasks_c, min_required_agents=6)
    assert is_healthy is False
    assert count == 0

    # Case D: partial_limit w/ usable evidence -> Accepted
    tasks_d = [
        {"agent_name": f"agent_{i}", "status": "partial_limit", "outputs_count": 10}
        for i in range(6)
    ]
    is_healthy, count = evaluate_collection_health(tasks_d, min_required_agents=6)
    assert is_healthy is True
    assert count == 6

    # Case E: browser failure, no evidence -> Rejected/Degraded
    tasks_e = [
        {"agent_name": f"agent_{i}", "status": "failed", "outputs_count": 0}
        for i in range(6)
    ]
    is_healthy, count = evaluate_collection_health(tasks_e, min_required_agents=6)
    assert is_healthy is False
    assert count == 0
