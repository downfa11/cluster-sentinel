# Sentinel 문서

## 개념

Sentinel은 명령어 봇이 아닙니다. Slack 자연어 메시지를 LLM이 해석하고, MCP 스타일 tool을 선택한 뒤, Policy Engine이 정확한 tool call과 인자를 검사합니다. 쓰기 작업은 GitHub Pull Request만 생성합니다.

## 구현된 Tool

- `github_create_deploy_pr`: Helm values의 image 필드를 패치하고 PR 생성
- `github_create_restart_pr`: restart annotation을 패치하고 PR 생성
- `github_create_rollback_pr`: rollback 대상 image 값으로 패치하고 PR 생성
- `github_create_onboard_pr`, `github_create_offboard_pr`, `github_create_grant_pr`, `github_create_revoke_pr`: `access/users.yaml`을 패치하는 리뷰 가능한 PR 생성
- `argocd_get_status`: Argo CD API 호출
- `argocd_diff`: Argo CD managed resource 조회
- `grafana_alerts`: Grafana alert API 호출
- `access_get_user`: 로컬 access metadata 조회 placeholder

Loki는 의도적으로 제외했습니다.

## 필수 런타임

Sentinel은 `SENTINEL_GEMINI_API_KEY` 또는 `SENTINEL_OPENAI_API_KEY`가 필요하며, 둘 다 있으면 Gemini를 우선합니다. 두 키 모두 없으면 tool을 추측해서 실행하지 않고 거절합니다.

## GitOps 쓰기 방식

기본 values 경로는 `apps/{service}/overlays/{environment}/values.yaml`입니다. Deploy/Rollback은 다음 값을 갱신합니다.

```yaml
image:
  repository: ghcr.io/example/api
  tag: v1
```

Restart는 다음 값을 갱신합니다.

```yaml
podAnnotations:
  sentinel.dev/restartedAt: <request-id>
```

## Access Sync

merge된 access 변경은 `sentinel-access-sync`가 반영합니다. GitHub team과 Grafana team은 실제 API로 동기화하고, Tailscale policy JSON은 생성한 뒤 credential이 있으면 Tailscale API로 publish하며, Argo CD RBAC CSV는 GitOps로 관리되는 Argo CD 설정에 넣을 수 있게 생성합니다.