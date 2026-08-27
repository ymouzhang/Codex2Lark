from __future__ import annotations

from dataclasses import dataclass

from .resources import LoadedResources
from .types import AgentDefinition, MessageRole, ModelMessage


@dataclass(frozen=True, slots=True)
class ContextEvidence:
    source_ref: str
    content: str
    source_version: str
    required: bool = False


@dataclass(frozen=True, slots=True)
class ContextBuild:
    messages: tuple[ModelMessage, ...]
    source_versions: dict[str, str]
    truncated_sources: tuple[str, ...]
    estimated_tokens: int
    journal_compacted: bool = False
    input_message_count: int = 0


class ContextEngine:
    def build(
        self,
        *,
        definition: AgentDefinition,
        resources: LoadedResources,
        user_request: str,
        evidence: tuple[ContextEvidence, ...],
        journal: tuple[ModelMessage, ...] = (),
    ) -> ContextBuild:
        stable = [
            ModelMessage(MessageRole.SYSTEM, definition.instructions, trusted=True),
            *(
                ModelMessage(MessageRole.SYSTEM, instruction, trusted=True)
                for instruction in resources.instructions
            ),
            *(
                ModelMessage(
                    MessageRole.SYSTEM,
                    f"Policy constraint: {policy}",
                    trusted=True,
                )
                for policy in resources.policies
            ),
        ]
        dynamic = [
            ModelMessage(
                MessageRole.USER,
                f"Untrusted source evidence [{item.source_ref}]:\n{item.content}",
            )
            for item in evidence
        ]
        request = ModelMessage(MessageRole.USER, user_request)
        messages = [*stable, *dynamic, request, *journal]
        input_message_count = len(messages)
        truncated: list[str] = []
        journal_compacted = False

        while self.estimate_tokens(messages) > definition.max_context_tokens:
            removable = next(
                (
                    index
                    for index, item in enumerate(evidence)
                    if not item.required and dynamic[index] in messages
                ),
                None,
            )
            if removable is not None:
                messages.remove(dynamic[removable])
                truncated.append(evidence[removable].source_ref)
                continue
            if journal:
                retained_evidence = [item for item in dynamic if item in messages]
                messages = self._compact_journal(stable, retained_evidence, request, journal)
                journal_compacted = True
                break
            raise ValueError("required context exceeds the AgentDefinition context capacity")

        estimated = self.estimate_tokens(messages)
        if estimated > definition.max_context_tokens:
            raise ValueError("compacted context exceeds the AgentDefinition context capacity")
        return ContextBuild(
            messages=tuple(messages),
            source_versions={item.source_ref: item.source_version for item in evidence},
            truncated_sources=tuple(truncated),
            estimated_tokens=estimated,
            journal_compacted=journal_compacted,
            input_message_count=input_message_count,
        )

    @staticmethod
    def estimate_tokens(messages: list[ModelMessage] | tuple[ModelMessage, ...]) -> int:
        return sum(max(1, (len(message.content) + 3) // 4) + 4 for message in messages)

    def _compact_journal(
        self,
        stable: list[ModelMessage],
        evidence: list[ModelMessage],
        request: ModelMessage,
        journal: tuple[ModelMessage, ...],
    ) -> list[ModelMessage]:
        keep_count = min(6, len(journal))
        cut = len(journal) - keep_count
        while cut > 0 and journal[cut].role is MessageRole.TOOL:
            cut -= 1
        old = journal[:cut]
        recent = journal[cut:]
        summary = self._structured_summary(old)
        compacted = [*stable, *evidence, request]
        if summary:
            compacted.append(ModelMessage(MessageRole.SYSTEM, summary, trusted=True))
        compacted.extend(recent)
        return compacted

    @staticmethod
    def _structured_summary(messages: tuple[ModelMessage, ...]) -> str:
        if not messages:
            return ""
        snippets = [f"{message.role.value}: {message.content[:240]}" for message in messages]
        return "Compacted complete prior turns (unverified claims remain claims):\n" + "\n".join(
            snippets
        )
