from __future__ import annotations

import heapq


class Node:
    def __init__(self, name: str) -> None:
        self.name = name
        self.successors: list[Node] = []

    def __lt__(self, other: Node) -> bool:
        return self.name < other.name


def _queued_distance(queue: list[tuple[float, Node]], wanted: Node) -> float | None:
    for distance, node in queue:
        if node is wanted:
            return distance
    return None


def _insert_or_update(queue: list[tuple[float, Node]], item: tuple[float, Node]) -> None:
    _, wanted = item
    for index, (_, node) in enumerate(queue):
        if node is wanted:
            queue[index] = item
            heapq.heapify(queue)
            return
    heapq.heappush(queue, item)


def shortest_path_length(
    length_by_edge: dict[tuple[Node, Node], float], start: Node, goal: Node
) -> float:
    queue: list[tuple[float, Node]] = [(0, start)]
    visited: set[Node] = set()
    while queue:
        distance, node = heapq.heappop(queue)
        if node is goal:
            return distance
        visited.add(node)
        for successor in node.successors:
            if successor in visited:
                continue
            queued = _queued_distance(queue, successor)
            previous = queued if queued is not None else float("inf")
            candidate = min(previous, distance + length_by_edge[node, successor])
            _insert_or_update(queue, (candidate, successor))
    return float("inf")
