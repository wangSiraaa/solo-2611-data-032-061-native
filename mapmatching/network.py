"""有向道路网络。

只有共享节点的边之间才能转向；几何上的交叉不构成路口。因此转向可行性完全
由 NetworkX 有向图的节点连通关系决定，几何 LineString 仅用于投影与长度。
"""

from __future__ import annotations

from dataclasses import dataclass

import networkx as nx

from .io import Road


@dataclass
class RoadNetwork:
    graph: nx.MultiDiGraph
    roads: dict  # edge_id -> Road

    @classmethod
    def build(cls, roads: list) -> "RoadNetwork":
        # 输入边按 edge_id 排序后入图，保证图的内部遍历次序与输入顺序无关。
        graph = nx.MultiDiGraph()
        ordered = sorted(roads, key=lambda r: r.edge_id)
        by_id: dict = {}
        for road in ordered:
            if road.edge_id in by_id:  # load 阶段已查，双保险
                raise ValueError(f"edge_id {road.edge_id} 重复")
            by_id[road.edge_id] = road
            graph.add_edge(
                road.from_node,
                road.to_node,
                key=road.edge_id,
                edge_id=road.edge_id,
                length_m=road.length_m,
                max_speed_mps=road.max_speed_mps,
                travel_time_s=road.length_m / road.max_speed_mps,
            )
        return cls(graph=graph, roads=by_id)

    def shortest_route(self, from_node: str, to_node: str):
        """以自由流旅行时间为权的最短路，返回 (edge_id 列表, 总长度米)。

        不存在有向路径时返回 None。等权路径由 NetworkX 的确定性节点/边遍历
        顺序决定，而图按 edge_id 排序构建，因此结果对输入顺序不敏感。
        """
        try:
            edge_seq = nx.shortest_path(
                self.graph,
                from_node,
                to_node,
                weight="travel_time_s",
            )
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None
        edge_ids: list = []
        total_length = 0.0
        for u, v in zip(edge_seq[:-1], edge_seq[1:]):
            data = self.graph.get_edge_data(u, v)
            if data is None:
                return None
            # MultiDiGraph 同一对节点可能有多条边；取确定性最小 key。
            key = min(data.keys())
            edge_ids.append(data[key]["edge_id"])
            total_length += data[key]["length_m"]
        return edge_ids, total_length
