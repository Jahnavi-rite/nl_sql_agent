from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class DebateTranscriptBuilder:
    turns: list[dict[str, Any]]
    metadata: dict[str, Any]

    def add_turn(self, entry: dict[str, Any]) -> None:
        self.turns.append(entry)

    def to_dict(self) -> dict[str, Any]:
        return {
            "turns": self.turns,
            "summary": {
                "total_turns": len(self.turns),
                "total_rounds": self.metadata.get("rounds", 0),
                "termination_reason": self.metadata.get("termination_reason"),
                "final_confidence": self.metadata.get("final_confidence"),
                "critic_score": self.metadata.get("critic_score"),
                "author_confidence": self.metadata.get("author_confidence"),
                "author_token_usage": self.metadata.get("author_token_usage", {}),
                "critic_token_usage": self.metadata.get("critic_token_usage", {}),
            },
        }
