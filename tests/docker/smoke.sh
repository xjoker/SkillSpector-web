#!/bin/sh
set -eu

IMAGE="${SKILLSPECTOR_DOCKER_IMAGE:-skillspector}"
REPO_DIR="${SKILLSPECTOR_REPO_DIR:-$(pwd)}"
LOCAL_REPORT="${SKILLSPECTOR_DOCKER_LOCAL_REPORT:-.skillspector-docker-smoke.json}"
GITHUB_REPORT="${SKILLSPECTOR_DOCKER_GITHUB_REPORT:-.skillspector-docker-github-smoke.json}"
GITHUB_URL="${SKILLSPECTOR_DOCKER_GITHUB_URL:-https://github.com/octocat/Hello-World}"
GITHUB_EXPECTED_COMPONENT="${SKILLSPECTOR_DOCKER_GITHUB_EXPECTED_COMPONENT:-README}"
REMOTE_SMOKE="${SKILLSPECTOR_DOCKER_REMOTE_SMOKE:-0}"

run() {
  printf "\n>> %s\n" "$*"
  "$@"
  return
}

validate_json_report() {
  report_path="$1"

  test -s "${REPO_DIR}/${report_path}"
  run docker run --rm --entrypoint python -v "${REPO_DIR}:/scan" "${IMAGE}" \
    -m json.tool "/scan/${report_path}" >/dev/null
  return
}

assert_report_contains_component() {
  report_path="$1"
  expected_component="$2"

  run docker run --rm --entrypoint python -v "${REPO_DIR}:/scan" "${IMAGE}" \
    -c 'import json, sys; data = json.load(open("/scan/" + sys.argv[1])); expected = sys.argv[2]; assert any(c.get("path") == expected for c in data.get("components", [])), f"missing component: {expected}"' \
    "${report_path}" "${expected_component}"
  return
}

smoke_web_health() {
  container_name="skillspector-web-smoke-$$"
  docker rm -f "${container_name}" >/dev/null 2>&1 || true

  run docker run -d --name "${container_name}" \
    -e SKILLSPECTOR_AUTH_TOKEN=smoke-token \
    "${IMAGE}" web --port 8765

  tries=0
  until docker exec "${container_name}" python -c 'import json, urllib.request; data = json.load(urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=2)); assert data["ok"] is True; assert data["service"] == "skillspector"; assert data["version"]; assert data["git_commit"]' 2>/dev/null; do
    tries=$((tries + 1))
    if [ "${tries}" -ge 10 ]; then
      docker logs "${container_name}"
      docker rm -f "${container_name}" >/dev/null 2>&1 || true
      echo "Web health smoke failed"
      exit 1
    fi
    sleep 1
  done

  if ! docker exec "${container_name}" python -c '
import json
import urllib.error
import urllib.request

base = "http://127.0.0.1:8765"
api_auth = {"Authorization": "Bearer smoke-token"}

def request(method, path, payload=None, headers=None):
    body = None
    request_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(base + path, data=body, headers=request_headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as res:
        return json.load(res)

try:
    urllib.request.urlopen(base + "/api/history", timeout=5)
    raise AssertionError("unauthenticated API request unexpectedly succeeded")
except urllib.error.HTTPError as exc:
    assert exc.code == 401, exc.code

ticket = request("POST", "/api/tickets", {"filename": "SKILL.md", "max_bytes": 1024}, api_auth)
upload_id = ticket["upload_id"]
upload_req = urllib.request.Request(
    ticket["upload_url"],
    data=b"# smoke skill",
    headers={**ticket["headers"], "Content-Length": "13"},
    method="PUT",
)
with urllib.request.urlopen(upload_req, timeout=20) as res:
    upload = json.load(res)
assert upload["ok"] is True
scan = request("POST", "/api/scans/" + upload_id, {"use_llm": False}, api_auth)
assert scan["ok"] is True
report = request("GET", "/api/reports/" + scan["report_id"], headers=api_auth)
assert report["ok"] is True
'; then
    docker logs "${container_name}"
    docker rm -f "${container_name}" >/dev/null 2>&1 || true
    echo "Web API smoke failed"
    exit 1
  fi

  docker rm -f "${container_name}" >/dev/null 2>&1 || true
  echo "Web health and API smoke passed"
  return
}

scan_github_url() {
  printf "\n>> docker run --rm -v %s:/scan %s scan %s --no-llm --format json --output /scan/%s\n" \
    "${REPO_DIR}" "${IMAGE}" "${GITHUB_URL}" "${GITHUB_REPORT}"

  set +e
  docker run --rm -v "${REPO_DIR}:/scan" "${IMAGE}" scan "${GITHUB_URL}" \
    --no-llm --format json --output "/scan/${GITHUB_REPORT}"
  github_scan_status="$?"
  set -e

  if [ "${github_scan_status}" -ne 0 ] && [ "${github_scan_status}" -ne 1 ]; then
    echo "GitHub URL scan failed with exit code ${github_scan_status}"
    exit "${github_scan_status}"
  fi

  validate_json_report "${GITHUB_REPORT}"
  assert_report_contains_component "${GITHUB_REPORT}" "${GITHUB_EXPECTED_COMPONENT}"
  echo "GitHub URL scan completed with accepted exit code ${github_scan_status}"
  return
}

run docker run --rm "${IMAGE}" --version
run docker run --rm --entrypoint git "${IMAGE}" --version
run docker run --rm "${IMAGE}" skillspector-web --help
run docker run --rm "${IMAGE}" skillspector-upload-mcp --help

run docker run --rm -v "${REPO_DIR}:/scan" "${IMAGE}" scan tests/fixtures/safe_skill \
  --no-llm --format json --output "/scan/${LOCAL_REPORT}"
validate_json_report "${LOCAL_REPORT}"
smoke_web_health

if [ "${REMOTE_SMOKE}" = "1" ]; then
  scan_github_url
else
  echo "Skipping GitHub URL smoke; set SKILLSPECTOR_DOCKER_REMOTE_SMOKE=1 to enable it."
fi
