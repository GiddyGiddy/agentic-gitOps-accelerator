from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from app.supervisor.state import EventState


def build_graph(checkpointer: BaseCheckpointSaver):
    def record(state: EventState) -> dict:
        return {
            "last_event_id": state["event_id"],
            "workflow_status": state["result"]["status"],
        }

    graph = StateGraph(EventState)
    graph.add_node("record", record)
    graph.add_edge(START, "record")
    graph.add_edge("record", END)
    return graph.compile(checkpointer=checkpointer)
