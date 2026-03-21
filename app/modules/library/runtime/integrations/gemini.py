"""Gemini client helpers and CLI wrapper."""

from google import genai
from google.genai import types
import subprocess
import os
import time

from core.paths import workdir


def gemini_cli(config, prompt):
    """Run the local Gemini CLI with the given prompt and config."""
    env = os.environ.copy()
    env["GEMINI_API_KEY"] = config['google_api_key']['free']

    command = [
        os.path.abspath(os.path.join(os.path.expanduser("~"), ".npm-global/bin/gemini")),
        "--model", "gemini-2.5-pro",
        "--prompt", prompt,
        "--yolo",
    ]
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
            env=env,
            cwd=os.path.abspath(os.path.expanduser(workdir))
        )
    except subprocess.CalledProcessError as e:
        print(f"Error: {e.stderr.strip()}")
        raise e


def create_client(api_key):
    """Create a Gemini API client."""
    return genai.Client(api_key=api_key)


def gemini_api(prompt, model, client, files={}, temperature=0.1, schema=None, timeout_sec=60 * 10):
    """Call Gemini API with optional file uploads and JSON schema."""
    uploaded_files = []
    for path, mime_type in files.items():
        _f = upload_and_wait(client, path, mime_type)
        uploaded_files.append(_f)
    prompt.extend(uploaded_files)
    resp_stream = client.models.generate_content_stream(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=temperature,
            response_mime_type="application/json",
            response_schema=schema,
            candidate_count=1,
            seed=1552,
            http_options=types.HttpOptions(timeout=timeout_sec * 1000),
        ),
    )
    return resp_stream, uploaded_files


def stream_text(resp_stream):
    """Extract concatenated text from Gemini stream chunks without using chunk.text."""
    parts_text = []
    for chunk in resp_stream:
        candidates = getattr(chunk, "candidates", None) or []
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            parts = getattr(content, "parts", None) or []
            for part in parts:
                text = getattr(part, "text", None)
                if text:
                    parts_text.append(text)
        # Backward-compatible fallback for unit tests that mock chunk as SimpleNamespace(text=...)
        if not candidates:
            raw = getattr(chunk, "__dict__", {}).get("text")
            if raw:
                parts_text.append(raw)
    return "".join(parts_text)


def upload_and_wait(client, path, mime_type, poll_interval=0.3, timeout=10):
    """Upload a file and wait until it becomes ACTIVE."""
    _f = client.files.upload(file=path, config={"mime_type": mime_type})
    waited = 0
    while True:
        f_state = client.files.get(name=_f.name)
        if f_state.state == "ACTIVE":
            return f_state
        time.sleep(poll_interval)
        waited += poll_interval
        if waited >= timeout:
            raise TimeoutError(f"File {path} did not become ACTIVE in time.")
