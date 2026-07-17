# Sentinel 문서

Sentinel은 Slack 자연어 기반 GitOps 에이전트입니다. Gemini 또는 OpenAI가 허용된 tool을 선택하고, Policy Engine이 서버에서 확정한 대상과 권한을 검사합니다. 쓰기 tool은 draft GitHub PR만 만들며 PR merge나 Kubernetes 직접 변경은 하지 않습니다.

## 구현된 기능

- 명시적으로 등록된 workload의 digest 고정 배포·롤백·재시작 draft PR
- `access/users.yaml`과 `external/tailscale/policy.hujson`을 함께 수정하는 온보딩·오프보딩·권한 변경 draft PR
- Argo CD 앱 목록, OutOfSync 목록, 상태, managed resources, 앱 소속 Pod 목록, 제한된 최근 Pod 로그 조회
- Slack에 실제 alert 내용을 표시하는 Grafana alert 조회
- GitHub의 access 파일을 이용한 비민감 사용자 권한 조회

Argo CD와 Grafana의 서비스명은 `SENTINEL_OPERATIONAL_TARGETS`에서 서버측으로 해석합니다. 사용자가 environment 문자열을 바꿔 다른 production 앱을 조회할 수 없습니다. 등록되지 않은 Slack 사용자는 역할이 없으며 거절됩니다.

## GitOps 쓰기

`SENTINEL_GITOPS_TARGETS`가 manifest 경로, image repository, Argo CD application, environment를 정의합니다. 배포와 롤백은 허용된 `repository@sha256:...` image 한 개만 변경하고, 재시작은 Deployment Pod template의 `sentinel.dev/restartedAt` annotation을 변경합니다. 실제 PR commit은 DCO sign-off를 포함하고 PR은 draft이며, 생성 실패 시 임시 branch를 제거합니다.

## Access sync

워크플로는 access 원본이 있는 `cluster-config`에 둡니다. `access/users.yaml`, `access/roles.yaml`, 리뷰된 Tailscale policy가 일치하는지 먼저 확인하고 정책을 publish합니다. Sentinel이 관리하지 않는 그룹과 나머지 정책 필드는 보존합니다.

채널에서는 bot mention이 필요합니다. DM은 기본 비활성화이며 `SENTINEL_SLACK_ALLOW_DMS=true`로 명시적으로 켤 수 있습니다.
