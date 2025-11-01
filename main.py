from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import requests

import requests
import streamlit as st
from openai import OpenAI

from system_prompts import (
    complexity_level as COMPLEXITY_SYSTEM_PROMPT,
    rubric_requirements_correctness as RUBRIC_FIX_SYSTEM_PROMPT,
    rubric_explanation as RUBRIC_EXPLANATION_PROMPT
)

MODEL_NAME = "gpt-5"
ENV_FILE_NAME = ".env"
APP_DIR = Path(__file__).resolve().parent
TASK_DATA_PATH = APP_DIR.parent / "approval_batch" / "approval_task_data.json"
EVALUATION_CHOICES: List[Tuple[str, str]] = [
    ("complexity_check", "Check complexity level"),
    ("rubric_explanation", "Generate rubric explanation (plain language, no bullets or markdown symbols)"),
    ("requirements_fixes", "Identify requirements that need improvement"),
]

def get_request_data(url):
  headers = {
      'Authorization': os.environ['API_TOKEN']
  }

  response = requests.get(url, headers=headers)

  converstions_data = None

  if response.status_code == 200:
      converstions_data = response.json()
      print("Data fetched successfully")
      return converstions_data
  else:
      print(f"Request failed with status code {response.status_code}")
      print(response.text)


def _load_env_file() -> None:
    """Populate os.environ using the first .env file discovered up the directory tree."""
    checked: set[Path] = set()
    for base in [Path(__file__).resolve().parent, *Path(__file__).resolve().parents]:
        if base in checked:
            continue
        checked.add(base)
        env_path = base / ENV_FILE_NAME
        if not env_path.exists():
            continue
        for raw_line in env_path.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key:
                continue
            if key in os.environ:
                continue
            cleaned = value.strip().strip("'").strip('"')
            os.environ[key] = cleaned
        break


_load_env_file()


def _get_secret(key: str, default: str = "") -> str:
    """Safely read a key from Streamlit secrets when available."""
    try:
        secrets = st.secrets  # type: ignore[attr-defined]
    except Exception:
        return default
    return secrets.get(key, default)


def _initialize_session_state() -> None:
    if "env_loaded" not in st.session_state:
        _load_env_file()
        st.session_state.env_loaded = True
    if "openai_api_key" not in st.session_state:
        st.session_state.openai_api_key = _get_secret("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY", ""))
    if "selected_task_id" not in st.session_state:
        st.session_state.selected_task_id: Optional[int] = None
    if "selected_evaluations" not in st.session_state:
        st.session_state.selected_evaluations: List[str] = []
    if "task_payload_cache" not in st.session_state:
        st.session_state.task_payload_cache: Dict[str, Any] = {}
    if "current_task_payload" not in st.session_state:
        st.session_state.current_task_payload = None
    if "evaluation_results" not in st.session_state:
        st.session_state.evaluation_results: Dict[str, Any] = {}
    if "task_id_input_field" not in st.session_state:
        st.session_state.task_id_input_field = ""


def _reset_task_payload() -> None:
    """Clear cached task data when the user changes the Task ID."""
    st.session_state.current_task_payload = None
    st.session_state.evaluation_results = {}


def get_conversation_data(conversation_id: Any, api_key: str) -> Dict[str, Any]:
    base_url = _get_secret("INSTANCE_URL", os.getenv("INSTANCE_URL", "")).strip()
    if not base_url:
        raise ValueError("INSTANCE_URL is not configured.")
    if not api_key or not api_key.strip():
        raise ValueError("API key is required to fetch conversation data.")

    if not base_url.endswith("/"):
        base_url = f"{base_url}/"

    url = f"{base_url}delivery/client/external/conversations/{conversation_id}"
    token = api_key.strip()
    if not token.lower().startswith("bearer "):
        token = f"Bearer {token}"

    try:
        response = requests.get(
            url,
            headers={"Authorization": token},
            timeout=30,
        )
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(f"Failed to fetch conversation {conversation_id}: {exc.response.status_code}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"Request error while fetching conversation {conversation_id}: {exc}") from exc

    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError("Conversation response was not valid JSON.") from exc


def _resolve_api_key() -> str:
    return (st.session_state.get("openai_api_key") or os.getenv("OPENAI_API_KEY", "")).strip()


def _call_model(messages: List[Dict[str, str]], api_key: str) -> str:
    client = OpenAI(api_key=api_key) if api_key else OpenAI()
    completion = client.chat.completions.create(model=MODEL_NAME, messages=messages)
    message = completion.choices[0].message
    return message.content or ""


def _build_complexity_user_payload(data: Mapping[str, Any], type_of_data=None) -> str:
    research_question = str(data.get("prompt", "") or "").strip()
    report_text = str(data.get("nova_response", "") or "").strip()
    rubric_entries = data.get("rubric_entries") or []
    if isinstance(rubric_entries, Mapping):
        rubric_entries = [rubric_entries]

    payload = {
            "Research Question": research_question,
            "Report Text": report_text,
            "Rubric Requirements": rubric_entries,
        }

    if type_of_data == "complexity_prompt":
        payload.update({
            "Report Text": report_text
        })
    return json.dumps(payload, ensure_ascii=False, indent=2)


def evaluate_complexity_level(data_to_render: Mapping[str, Any], api_key: str, system_prompt, user_payload) -> Dict[str, Any]:
    if not api_key or not api_key.strip():
        raise ValueError("OpenAI API key is required to run the complexity evaluation.")

    response_text = _call_model(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_payload},
        ],
        api_key=api_key,
    )

    try:
        return json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Model response is not valid JSON: {response_text}") from exc


def evaluate_requirements_fixes(data_to_render: Mapping[str, Any], api_key: str) -> List[Dict[str, Any]]:
    if not api_key or not api_key.strip():
        raise ValueError("OpenAI API key is required to run the requirements evaluation.")

    research_question = str(data_to_render.get("prompt", "") or "").strip()
    rubric_entries = data_to_render.get("rubric_entries") or []
    if isinstance(rubric_entries, Mapping):
        rubric_entries = [rubric_entries]

    payload = {
        "Research Question": research_question,
        "Rubric Requirements": rubric_entries,
    }

    response_text = _call_model(
        [
            {"role": "system", "content": RUBRIC_FIX_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
        ],
        api_key=api_key,
    )

    try:
        parsed = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Model response is not valid JSON: {response_text}") from exc

    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    raise RuntimeError(f"Unexpected response structure for requirements fixes: {response_text}")


@st.cache_data(show_spinner=False)
def _load_tasks(task_path: Path) -> List[Dict[str, Any]]:
    if not task_path.exists():
        return []
    try:
        payload = json.loads(task_path.read_text())
    except json.JSONDecodeError:
        return []

    tasks: List[Dict[str, Any]] = []
    for entry in payload:
        conversation_id = entry.get("conversation_id")
        metadata = entry.get("metadata", {})
        domain = ""
        scope = metadata.get("scope_requirement") if isinstance(metadata, dict) else {}
        if isinstance(scope, dict):
            domain = scope.get("domain") or scope.get("suggested-domain") or ""
        project_name = metadata.get("project_name", "") if isinstance(metadata, dict) else ""
        tasks.append(
            {
                "conversation_id": conversation_id,
                "domain": domain,
                "project": project_name,
            }
        )
    return tasks


def _format_task_option(task: Dict[str, Any]) -> str:
    conversation_id = task.get("conversation_id")
    domain = task.get("domain") or "Unknown domain"
    label_parts = [f"Task {conversation_id}" if conversation_id is not None else "Task"]
    label_parts.append(domain)
    project = task.get("project")
    if project:
        label_parts.append(f"({project})")
    return " – ".join(label_parts)


def _get_data_to_render(fetched_data: Mapping[str, Any]) -> Dict[str, Any]:
    prompt_text = ""
    nova_response = ""
    rubric_entries: List[Dict[str, Any]] = []
    evaluation_instruction = ""
    annotator_complexity_level = ""
    annotator_domain = ""

    messages = fetched_data.get("messages")
    if isinstance(messages, list):
        # Research question prompt typically resides in the second message.
        if len(messages) > 1 and isinstance(messages[1], Mapping):
            prompt_text = str(messages[1].get("text", "") or "")

        if len(messages) > 2 and isinstance(messages[2], Mapping):
            assistant_message = messages[2]
            response_options = assistant_message.get("response_options")
            if isinstance(response_options, list):
                for option in response_options:
                    if not isinstance(option, Mapping):
                        continue
                    model_id = str(option.get("model_id", "") or "")
                    text = str(option.get("text", "") or "")
                    if "us.amazon.nova-pro-v1" in model_id:
                        nova_response = text
                        break
                    if not nova_response:
                        nova_response = text

            signal = assistant_message.get("signal")
            if isinstance(signal, Mapping):
                preference_evals = signal.get("preference_evals")
                if isinstance(preference_evals, Mapping):
                    evaluation_form = preference_evals.get("evaluation_form")
                    if isinstance(evaluation_form, list):
                        rubric_entries = evaluation_form[0].get("human_input_value")
                        evaluation_instruction = evaluation_form[1].get("human_input_value")

                prompt_evals = signal.get("prompt_evals")
                if isinstance(prompt_evals, Mapping):
                    prompt_evaluation_form = prompt_evals.get("evaluation_form")
                    if isinstance(prompt_evaluation_form, list):
                        if len(prompt_evaluation_form) > 0 and isinstance(prompt_evaluation_form[0], Mapping):
                            annotator_domain = str(
                                prompt_evaluation_form[0].get("human_input_value", "") or ""
                            )
                        if len(prompt_evaluation_form) > 2 and isinstance(prompt_evaluation_form[2], Mapping):
                            annotator_complexity_level = str(
                                prompt_evaluation_form[2].get("human_input_value", "") or ""
                            )

    return {
        "prompt": prompt_text,
        "nova_response": nova_response,
        "rubric_entries": rubric_entries,
        "evaluation_instruction": evaluation_instruction,
        "annotator_complexity_level": annotator_complexity_level,
        "annotator_domain": annotator_domain,
    }


def main() -> None:
    st.set_page_config(page_title="Reviewer", page_icon="🤖")
    _initialize_session_state()

    st.title("Reviewer")
    st.caption("Provide credentials, select a task, and choose the checks you want to run.")

    with st.sidebar:
        st.header("Configuration")
        lt_api_key = st.text_input(
            "Your Labeling Tool API Key",
            key="",
            type="password",
            help="Leave blank to use the OPENAI_API_KEY environment variable.",
        )
        st.caption("API key is stored in session state only for the current browser session.")

    st.subheader("Task Selection")

    default_task_id = ""
    if st.session_state.selected_task_id is not None:
        default_task_id = str(st.session_state.selected_task_id)

    if default_task_id and not st.session_state.task_id_input_field:
        st.session_state.task_id_input_field = default_task_id

    st.text_input(
        "Task ID",
        key="task_id_input_field",
        placeholder="e.g. 285230",
        help="Conversation/task identifier to evaluate.",
        on_change=_reset_task_payload,
    )

    task_id_input = st.session_state.task_id_input_field

    fetched_payload: Optional[Dict[str, Any]] = None
    processed_payload: Optional[Dict[str, Any]] = None
    normalized_task_id = task_id_input.strip() if task_id_input else ""

    if normalized_task_id:
        st.session_state.selected_task_id = normalized_task_id
        if lt_api_key:
            payload_cache = st.session_state.task_payload_cache
            fetched_payload = payload_cache.get(normalized_task_id)
            if fetched_payload is None:
                with st.spinner("Fetching conversation data..."):
                    try:
                        fetched_payload = get_conversation_data(normalized_task_id, lt_api_key)
                    except Exception as error:  # noqa: BLE001
                        st.error(f"Conversation fetch failed: {error}")
                        fetched_payload = None
                    else:
                        payload_cache[normalized_task_id] = fetched_payload
            if fetched_payload is not None:
                processed_payload = _get_data_to_render(fetched_payload)
                st.session_state.current_task_payload = processed_payload
                st.session_state.evaluation_results = {}
        else:
            st.warning("Provide an API key to fetch conversation data.")
    else:
        st.session_state.selected_task_id = None
        st.session_state.current_task_payload = None
        st.session_state.evaluation_results = {}

    if processed_payload is None:
        processed_payload = st.session_state.get("current_task_payload")

    if processed_payload:
        with st.expander("Conversation payload preview", expanded=False):
            st.json(processed_payload)


    st.subheader("Evaluation Scope")
    choice_labels = [label for _, label in EVALUATION_CHOICES]
    default_choices = [
        label for value, label in EVALUATION_CHOICES if value in st.session_state.selected_evaluations
    ]
    selected_labels = st.multiselect(
        "Select one evaluation to run",
        choice_labels,
        default=default_choices[:1],
        help="Only one evaluation can run at a time.",
        max_selections=1,
    )
    print(selected_labels)

    label_to_value = {label: value for value, label in EVALUATION_CHOICES}
    st.session_state.selected_evaluations = [label_to_value[label] for label in selected_labels]

    run_requested = st.button("Run selected evaluations", type="primary", use_container_width=True)

    api_key = _resolve_api_key()
    data_to_render = st.session_state.get("current_task_payload")

    if run_requested:
        if not task_id_input:
            st.error("Enter a Task ID before running evaluations.")
        elif not st.session_state.selected_evaluations:
            st.error("Select at least one evaluation to run.")
        elif not api_key:
            st.error("Provide an OpenAI API key to contact GPT-5.")
        elif not isinstance(data_to_render, Mapping):
            st.error("Conversation payload is missing or invalid.")
        else:
            eval_results: Dict[str, Any] = {}
            with st.spinner("Running evaluations ..."):
                try:
                    
                    if "complexity_check" in st.session_state.selected_evaluations:
                        user_payload = _build_complexity_user_payload(data_to_render, "complexity_propt")
                        eval_results["complexity_check"] = evaluate_complexity_level(data_to_render, api_key, COMPLEXITY_SYSTEM_PROMPT, user_payload)
                    elif "requirements_fixes" in st.session_state.selected_evaluations:
                        user_payload = _build_complexity_user_payload(data_to_render, "requirement_prompt")
                        eval_results["complexity_check"] = evaluate_complexity_level(data_to_render, api_key, RUBRIC_FIX_SYSTEM_PROMPT, user_payload)
                    elif "rubric_explanation" in st.session_state.selected_evaluations:
                        user_payload = _build_complexity_user_payload(data_to_render, "rubric_explanation")
                        eval_results["complexity_check"] = evaluate_complexity_level(data_to_render, api_key, RUBRIC_EXPLANATION_PROMPT, user_payload)
                except Exception as error:  # noqa: BLE001
                    st.error(f"Evaluation failed: {error}")
                else:
                    st.session_state.evaluation_results = eval_results

    results: Dict[str, Any] = st.session_state.get("evaluation_results", {})

    if results.get("complexity_check"):
        st.subheader("Complexity Check")
        st.json(results["complexity_check"])


if __name__ == "__main__":
    main()
