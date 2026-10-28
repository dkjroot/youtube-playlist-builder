# Adapted from Iris by Dave and contributors; GPL-3.0-only. See LICENSE here.
"""The small chat API shared by KoboldCPP, llama.cpp and other local servers."""

from __future__ import annotations

import html
import json
import time
from urllib.parse import urlsplit
from ...diagnostics import logger, provider_error, safe_text
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from typing import Any as Settings


class ChatError(RuntimeError):
    """A failed turn which leaves the conversation history alone."""


class Message(BaseModel):
    """Validate role/content before building requests or accepting provider responses."""
    model_config = ConfigDict(strict=True)
    role: Literal["system", "user", "assistant"]
    content: str


class Choice(BaseModel):
    """Model the text-message portion of one compatible API response choice."""
    message: Message


class ChatResponse(BaseModel):
    """Require at least one response choice before reading the assistant reply."""
    choices: list[Choice] = Field(min_length=1)


class ChatEngine:
    """The adapted Iris text chat transport, shared by local and hosted compatible endpoints."""
    def __init__(self, settings: Settings, client: httpx.Client) -> None:
        """Accept settings and caller-owned HTTP client; classification config disables history."""
        self.settings = settings
        self.client = client
        self.history: list[Message] = []

    def get_response(self, user_text: str) -> str:
        """Send one non-streaming turn, validate text and update history only after success.

        Logs contain bounded request metadata and redacted error fields, not prompt
        bodies or successful replies. The caller handles budgets and response JSON.
        """
        if not user_text.strip():
            raise ChatError("There isn't any text to send")
        messages = [
            Message(role="system", content=self.settings.system_prompt),
            *self.history,
            Message(role="user", content=user_text),
        ]
        payload = {
            "model": self.settings.model,
            "messages": [message.model_dump() for message in messages],
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
            "stream": False,
        }
        if self.settings.trace:
            print("Sending:", json.dumps(payload, ensure_ascii=False))
        headers = {}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key.get_secret_value()}"
        secrets = [self.settings.api_key.get_secret_value()] if self.settings.api_key else []
        logger.info('LLM request host=%s model=%s temperature=%s max_tokens=%s messages=%s input_chars=%s auth_present=%s',
                    urlsplit(self.settings.base_url).hostname, safe_text(self.settings.model, secrets),
                    self.settings.temperature, self.settings.max_tokens, len(messages),
                    sum(len(message.content) for message in messages), bool(secrets))
        started = time.monotonic()
        try:
            response = self.client.post(
                f"{self.settings.base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=httpx.Timeout(self.settings.timeout, connect=10),
            )
            response.raise_for_status()
            reply = ChatResponse.model_validate_json(response.content).choices[0].message
        except httpx.TimeoutException as error:
            logger.error('LLM request timed out after %.2fs', time.monotonic() - started)
            raise ChatError(
                "The model took too long to reply. Check the server or timeout setting."
            ) from error
        except httpx.HTTPStatusError as error:
            logger.error('LLM HTTP %s elapsed=%.2fs provider_error=%s', error.response.status_code,
                         time.monotonic() - started, provider_error(error.response, secrets))
            raise ChatError(
                f"The chat server returned HTTP {error.response.status_code}. "
                "See the local runtime.log for provider details."
            ) from error
        except httpx.RequestError as error:
            logger.error('LLM transport failure: %s', type(error).__name__)
            raise ChatError(
                "Couldn't reach the chat server. Is it running at the configured address?"
            ) from error
        except ValidationError as error:
            logger.error('LLM response validation failed (body omitted)')
            raise ChatError(
                "The server didn't return a text chat reply in the expected format."
            ) from error
        logger.info('LLM response HTTP %s elapsed=%.2fs output_chars=%s', response.status_code,
                    time.monotonic() - started, len(reply.content))
        text = reply.content
        # Modern chat APIs return plain text. Only decode for a server explicitly
        # configured to escape HTML; otherwise an explanation of &#x27; must survive.
        passes = {"text": 0, "html": 1, "double-html": 2}[self.settings.reply_encoding]
        for _ in range(passes):
            text = html.unescape(text)
        if reply.role != "assistant" or not text.strip():
            raise ChatError("The server returned an empty or invalid assistant reply")
        if self.settings.trace:
            print(f"Received ({time.monotonic() - started:.2f}s):", text)
        self.history.extend([messages[-1], Message(role="assistant", content=text)])
        limit = self.settings.history_turns * 2
        self.history = self.history[-limit:] if limit else []
        return text
