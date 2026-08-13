from __future__ import annotations

import random

import pytest
from shortest_path_length import Node, shortest_path_length


def _oracle(edges: dict[tuple[Node, Node], float], start: Node, goal: Node) -> float:
    nodes = {node for edge in edges for node in edge} | {start, goal}
    best = {node: float("inf") for node in nodes}
    best[start] = 0.0
    for _ in range(max(0, len(nodes) - 1)):
        changed = False
        for (source, target), weight in edges.items():
            candidate = best[source] + weight
            if candidate < best[target]:
                best[target] = candidate
                changed = True
        if not changed:
            break
    return best[goal]


def _graph(
    spec: list[tuple[str, str, float]], start_name: str, goal_name: str
) -> tuple[dict[tuple[Node, Node], float], Node, Node]:
    names = {name for edge in spec for name in edge[:2]} | {start_name, goal_name}
    nodes = {name: Node(name) for name in names}
    edges: dict[tuple[Node, Node], float] = {}
    for source, target, weight in spec:
        nodes[source].successors.append(nodes[target])
        edges[nodes[source], nodes[target]] = weight
    return edges, nodes[start_name], nodes[goal_name]


@pytest.mark.parametrize(
    ("spec", "start", "goal"),
    [
        ([("a", "b", 4)], "a", "b"),
        ([("a", "b", 2), ("b", "c", 3)], "a", "c"),
        ([("a", "b", 2), ("a", "c", 1), ("c", "b", 1)], "a", "b"),
        ([("a", "b", 8), ("a", "c", 2), ("c", "d", 2), ("d", "b", 1)], "a", "b"),
        ([("a", "b", 1), ("x", "y", 1)], "a", "y"),
        ([], "same", "same"),
    ],
)
def test_matches_independent_oracle(
    spec: list[tuple[str, str, float]], start: str, goal: str
) -> None:
    edges, start_node, goal_node = _graph(spec, start, goal)
    assert shortest_path_length(edges, start_node, goal_node) == _oracle(
        edges, start_node, goal_node
    )


def test_matches_oracle_for_deterministically_generated_graphs() -> None:
    generator = random.Random(20260717)
    for graph_number in range(12):
        nodes = [Node(f"g{graph_number}-n{index}") for index in range(6)]
        edges: dict[tuple[Node, Node], float] = {}
        for index in range(len(nodes) - 1):
            source, target = nodes[index], nodes[index + 1]
            source.successors.append(target)
            edges[source, target] = float(generator.randint(1, 9))
        for source_index in range(len(nodes) - 2):
            for target_index in range(source_index + 2, len(nodes)):
                if generator.random() < 0.45:
                    source, target = nodes[source_index], nodes[target_index]
                    source.successors.append(target)
                    edges[source, target] = float(generator.randint(1, 12))

        assert shortest_path_length(edges, nodes[0], nodes[-1]) == _oracle(
            edges, nodes[0], nodes[-1]
        )


def test_zero_weight_frontier_distance_is_not_an_absence_sentinel() -> None:
    edges, start, goal = _graph(
        [("start", "zero", 0), ("zero", "goal", 5), ("start", "goal", 10)],
        "start",
        "goal",
    )
    assert shortest_path_length(edges, start, goal) == _oracle(edges, start, goal) == 5
