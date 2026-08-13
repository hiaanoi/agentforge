from shortest_path_length import Node, shortest_path_length


def test_competing_routes_choose_the_shortest_total_distance() -> None:
    start, slow, fast, goal = (Node(name) for name in ("start", "slow", "fast", "goal"))
    start.successors = [slow, fast]
    fast.successors = [slow]
    slow.successors = [goal]
    edges = {(start, slow): 5, (start, fast): 1, (fast, slow): 1, (slow, goal): 1}

    assert shortest_path_length(edges, start, goal) == 3
