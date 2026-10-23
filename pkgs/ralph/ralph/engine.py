"""Common review-engine case, runtime, output, and local loader."""

import hashlib
import importlib
import json
from dataclasses import dataclass
from pathlib import Path

from .repository import RepositorySnapshot

_DEFAULT_BUDGET = object()


@dataclass(frozen=True)
class ReviewCase:
    review: str
    snapshot: RepositorySnapshot
    source: dict | None = None
    research_evidence: object | None = None


@dataclass
class Output:
    content: str
    data: dict
    exports: dict
    schema_version: int = 1


class Runtime:
    """Per-run access to model requests, spend, cancellation, and diagnostics."""

    def __init__(
        self,
        *,
        case,
        api_key,
        ppq_api_key=None,
        budget=None,
        ppq_budget=None,
        is_current=None,
        discussion_lookup=None,
        allow_web_search=False,
        attempt=None,
    ):
        from . import research

        research.validate(case.research_evidence)
        self.case = case
        self.api_key = api_key
        self.ppq_api_key = ppq_api_key
        self.budget = budget
        self.ppq_budget = ppq_budget
        self.is_current = is_current
        self.discussion_lookup = discussion_lookup
        self.allow_web_search = allow_web_search
        self.attempt = attempt if attempt is not None else {}
        self.debug = self.attempt
        self._records = []

    def check_current(self):
        if self.is_current is not None and not self.is_current():
            from .model import StaleReview

            raise StaleReview()

    def check_cancelled(self):
        self.check_current()

    def review(
        self,
        stage_name,
        input_text,
        *,
        prompt,
        model_name,
        tools,
        max_tool_calls,
        max_output_tokens,
        reasoning_effort,
        response_schema,
        record=None,
        first_tool_required=False,
        research_evidence=None,
        provider="openai",
        budget=_DEFAULT_BUDGET,
        api_base=None,
        timeout=180,
        final_timeout=180,
    ):
        from .model import openai_review

        record = record if record is not None else {}
        self._records.append(record)
        is_ppq = provider == "ppq"
        api_key = self.ppq_api_key if is_ppq else self.api_key
        selected_budget = (
            (self.ppq_budget if is_ppq else self.budget)
            if budget is _DEFAULT_BUDGET
            else budget
        )
        selected_api_base = api_base or (
            "https://api.ppq.ai/v1" if is_ppq else "https://api.openai.com/v1"
        )
        return openai_review(
            api_key,
            input_text,
            self.case.snapshot,
            record,
            prompt=prompt,
            model=model_name,
            tools=tools,
            max_tool_calls=max_tool_calls,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            first_tool_required=first_tool_required,
            stage_name=stage_name,
            budget=selected_budget,
            response_schema=response_schema,
            discussion_lookup=self.discussion_lookup,
            allow_web_search=self.allow_web_search,
            is_current=self.is_current,
            **(
                {"api_base": selected_api_base}
                if selected_api_base != "https://api.openai.com/v1"
                else {}
            ),
            timeout=timeout,
            final_timeout=final_timeout,
            research_evidence=(
                self.case.research_evidence
                if self.case.research_evidence is not None
                else research_evidence
            ),
        )

    def audit(
        self,
        stage_name,
        prompt,
        input_text,
        *,
        model_name,
        max_output_tokens,
        response_schema,
        reasoning_effort="low",
        debug_record=None,
    ):
        from .model import run_audit

        debug_record = debug_record if debug_record is not None else {}
        self._records.append(debug_record)
        return run_audit(
            self.api_key,
            stage_name,
            prompt,
            input_text,
            budget=self.budget,
            response_schema=response_schema,
            reasoning_effort=reasoning_effort,
            is_current=self.is_current,
            debug=debug_record,
            model_name=model_name,
            max_output_tokens=max_output_tokens,
        )

    def metrics(self):
        from .trace import request_metrics

        return request_metrics(self._records)


def _canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _code_hash(module):
    package_dir = Path(__file__).parent
    digest = hashlib.sha256()
    for path in sorted(package_dir.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    path = Path(module.__file__)
    digest.update(path.name.encode())
    digest.update(b"\0")
    digest.update(path.read_bytes())
    return digest.hexdigest()


@dataclass(init=False)
class LoadedEngine:
    name: str
    module: object
    _config_json: str
    _identity: dict

    def __init__(self, name, module, config):
        self.name = name
        self.module = module
        self._config_json = _canonical_json(config)
        self._identity = {
            "name": self.name,
            "code_sha256": _code_hash(self.module),
            "config_sha256": hashlib.sha256(self._config_json.encode()).hexdigest(),
            "config": json.loads(self._config_json),
        }

    @property
    def config(self):
        return json.loads(self._config_json)

    @property
    def identity(self):
        return {**self._identity, "config": json.loads(self._config_json)}

    def run(self, case, runtime):
        output = self.module.run(case, runtime, self.config)
        if not isinstance(output, Output):
            raise TypeError("engine run must return Output")
        if (
            not isinstance(output.content, str)
            or not isinstance(output.data, dict)
            or not isinstance(output.exports, dict)
            or type(output.schema_version) is not int
            or output.schema_version < 1
        ):
            raise TypeError("invalid engine output envelope")
        return output


def load(name="current", config=None, *, configured=False):
    if (
        not isinstance(name, str)
        or not name
        or any(not part.isidentifier() for part in name.split("."))
    ):
        raise ValueError("engine name must be a Python module name")
    module_name = "ralph.current_engine" if name == "current" else name
    module = importlib.import_module(module_name)
    normalized = config if configured else module.configure(config or {})
    if not isinstance(normalized, dict):
        raise TypeError("engine configuration must be a JSON object")
    _canonical_json(normalized)
    return LoadedEngine(name, module, normalized)
