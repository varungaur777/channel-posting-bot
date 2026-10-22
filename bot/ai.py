"""Optional AI caption helper (Claude API). OFF unless ANTHROPIC_API_KEY is set.

Given a caption it returns 2-3 rewritten options the submitter can pick from in the DM preview:
  improve   - catchier wording, same language, same facts
  translate - Hindi/Hinglish <-> English
  hashtags  - the caption plus a few relevant hashtags
Nothing is posted automatically; the submitter always chooses.
"""

from __future__ import annotations

import json
import logging

import anthropic

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
MODES = ("improve", "translate", "hashtags")

_SYSTEM = (
    "You help the admins of a Telegram channel polish post captions. You get one caption and a task, and "
    "you return 3 alternative versions of that caption.\n"
    "Rules for every version:\n"
    "- Keep every fact, name, number, date, link and @mention exactly as given; never invent details.\n"
    "- Plain text only: no Markdown, no HTML, no surrounding quotes. Line breaks are fine.\n"
    "- Put a short, strong headline on the first line - the channel shows the first line in bold.\n"
    "- Emojis are fine where they fit the tone; do not overdo them.\n"
    "- Each version must be at most {limit} characters.\n"
)
_TASKS = {
    "improve": "Make the caption more engaging and easier to read, in the SAME language and script as the "
               "original (Hindi, Hinglish or English). Vary the three versions: one close to the original, "
               "one punchier, one more detailed.",
    "translate": "Translate the caption: if it is mainly Hindi or Hinglish, translate it to natural English; "
                 "if it is mainly English, translate it to natural Hindi in Devanagari. Give three slightly "
                 "different natural translations.",
    "hashtags": "Keep the caption as it is (fix only obvious typos) and add 3-6 relevant hashtags on the "
                "last line. Vary the hashtag choice across the three versions.",
}
_SCHEMA = {
    "type": "object",
    "properties": {"options": {"type": "array", "items": {"type": "string"}}},
    "required": ["options"],
    "additionalProperties": False,
}


class AIUnavailable(Exception):
    """The AI helper could not produce suggestions (message is safe to show to the user)."""


class CaptionAI:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL) -> None:
        self.model = model or DEFAULT_MODEL
        self.client = (anthropic.AsyncAnthropic(api_key=api_key, timeout=90.0, max_retries=2)
                       if api_key else None)

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def suggest(self, caption: str, mode: str, *, limit: int = 1024) -> list[str]:
        if self.client is None:
            raise AIUnavailable("The AI helper is off (no ANTHROPIC_API_KEY).")
        if mode not in MODES:
            raise AIUnavailable(f"Unknown AI mode {mode!r}.")
        try:
            response = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=8000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",  # if the model declines, the API retries on Anthropic's recommended model
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA}},
                system=_SYSTEM.format(limit=limit),
                messages=[{"role": "user", "content": f"Task: {_TASKS[mode]}\n\nCaption:\n{caption}"}],
            )
        except anthropic.AuthenticationError as e:
            log.error("AI helper: invalid ANTHROPIC_API_KEY (%s)", e.message)
            raise AIUnavailable("The AI key is not valid – ask the owner to check ANTHROPIC_API_KEY.") from None
        except anthropic.PermissionDeniedError as e:
            log.error("AI helper: permission denied (%s)", e.message)
            raise AIUnavailable("The AI key does not have access to this model.") from None
        except anthropic.RateLimitError:
            raise AIUnavailable("The AI helper is busy right now – try again in a minute.") from None
        except anthropic.BadRequestError as e:
            log.error("AI helper: bad request (%s)", e.message)
            raise AIUnavailable("The AI helper could not process this caption.") from None
        except anthropic.APIStatusError as e:
            log.warning("AI helper: API error %s (%s)", e.status_code, e.message)
            raise AIUnavailable("The AI service had a problem – try again later.") from None
        except anthropic.APIConnectionError:
            raise AIUnavailable("Could not reach the AI service – try again later.") from None

        if response.stop_reason == "refusal":
            raise AIUnavailable("The AI declined to rewrite this caption.")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            options = json.loads(text)["options"]
        except (ValueError, KeyError, TypeError):
            log.warning("AI helper: unexpected output %r (stop_reason=%s)", text[:200], response.stop_reason)
            raise AIUnavailable("The AI returned something unexpected – try again.") from None
        cleaned = []
        for opt in options:
            opt = str(opt).strip()
            if opt and len(opt) <= limit and opt not in cleaned:
                cleaned.append(opt)
        if not cleaned:
            raise AIUnavailable("The AI did not return a usable caption – try again.")
        return cleaned[:3]
