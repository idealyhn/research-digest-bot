# Research Digest Bot

NVIDIA, Figure AI, Physical Intelligence, Google DeepMind 등 로봇·AI 연구 조직의 새 소식을
매일 아침 08:00(KST)에 Slack 채널로 보내 주는 봇입니다. 요약은 Claude API가 작성합니다.

- **수집 대상**: 공식 블로그·뉴스룸, YouTube 채널, arXiv 신규 논문, GitHub 신규 저장소·릴리스, Hugging Face 모델·데이터셋
- **중복 없음**: 이미 보낸 항목은 `state/seen.json`에 기록되므로 다시 보내지 않습니다
- **요약 품질**: 제목만이 아니라 원문 본문, 초록, README를 가져와 요약합니다
- **자체 점검**: 실패한 소스는 다이제스트 하단에 표시됩니다

```
GitHub Actions (매일 07:40 KST 시작)
  └─ python -m digest --wait
       1. collect   RSS · YouTube · arXiv · GitHub · HF · 뉴스 목록 페이지
       2. dedupe    state/seen.json 과 비교해 새 항목만 추림
       3. arXiv     watchlist 매칭 → 소속 확인 → Claude가 주목할 논문 추가 선정
       4. enrich    원문 본문 / README / 모델 카드 수집
       5. summarize Claude: 1–2문장 요약 + 중요도(1–5) + 오늘의 하이라이트
       6. post      08:00까지 대기 후 Slack 게시 → state 커밋
```

## 1. 설정 (약 20–30분)

### 1-1. 저장소 만들기
이 폴더 전체를 GitHub 저장소로 올립니다. 연구실 organization 아래에 두는 것을 권장합니다.
Actions 사용량 제한을 고려하면 공개(public) 저장소가 가장 간단합니다.
비공개 저장소는 월 2,000분 무료 한도 안에서 충분히 돌아갑니다(하루 약 5–25분).

### 1-2. Slack Incoming Webhook 만들기
1. <https://api.slack.com/apps> → **Create New App** → *From scratch* → 이름 예: `Research Digest`, 워크스페이스 선택
2. 왼쪽 메뉴 **Incoming Webhooks** → **Activate** 켜기
3. **Add New Webhook to Workspace** → 게시할 채널 선택(예: `#robotics-digest`) → 허용
4. 생성된 `https://hooks.slack.com/services/...` URL 복사

> 게시물 여러 개를 스레드로 묶고 싶다면 Webhook 대신 봇 토큰을 쓸 수 있습니다.
> **OAuth & Permissions**에서 `chat:write` 권한을 주고 `xoxb-...` 토큰을 받은 뒤, 봇을 채널에 초대하세요.

### 1-3. Claude API 키
<https://console.anthropic.com> → **API Keys**에서 키를 발급합니다.
결제 수단을 등록해야 하며, 사용량 한도(예: 월 $20)를 걸어 두는 것을 권장합니다.

### 1-4. GitHub Secrets / Variables 등록
저장소 → **Settings → Secrets and variables → Actions**

| 종류 | 이름 | 값 |
|---|---|---|
| Secret | `SLACK_WEBHOOK_URL` | 1-2의 Webhook URL |
| Secret | `ANTHROPIC_API_KEY` | 1-3의 API 키 |
| (선택) Secret | `SLACK_BOT_TOKEN` | Webhook 대신 봇 토큰을 쓸 때 |
| (선택) Variable | `SLACK_CHANNEL` | 봇 토큰 사용 시 채널 ID (`C0...`) |
| (선택) Variable | `ANTHROPIC_MODEL` | 기본값 `claude-sonnet-5-5`. 더 깊은 요약이 필요하면 `claude-opus-5-5` |

**Settings → Actions → General → Workflow permissions**에서
*Read and write permissions*를 선택해야 봇이 `state/seen.json`을 커밋할 수 있습니다.

### 1-5. 첫 실행 (부트스트랩)
**Actions → Daily research digest → Run workflow**를 누릅니다.

- 기본 실행(`backfill` 끔): 모든 소스의 **현재 항목을 "이미 본 것"으로 기록만** 하고 Slack에는 보내지 않습니다.
  다음 날 아침부터 새 항목만 옵니다. 첫날 채널이 수백 개 항목으로 뒤덮이는 것을 막기 위한 동작입니다.
- `backfill` 켬: 소스마다 최신 항목을 최대 3개씩(arXiv는 주목 논문) 담은 **샘플 다이제스트를 바로** 보냅니다.
  설정이 제대로 됐는지 확인할 때 좋습니다.
- `dry_run` 켬: Slack에 게시하지도, 상태를 저장하지도 않습니다. 결과는 실행 페이지의 Artifacts(`digest-run.json`)에서 확인할 수 있습니다.

이후에는 매일 07:40(KST)에 자동으로 시작해서 **08:00에 게시**됩니다.

## 2. 로컬 실행

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 값 채우기
set -a; source .env; set +a

python -m digest --dry-run --backfill --print          # 게시하지 않고 Slack 페이로드 출력
python -m digest --dry-run --no-llm --source figure-news --backfill -v   # 소스 하나만 점검
python -m pytest -q                                      # 테스트
```

`--print`로 출력된 JSON의 `blocks` 부분을 [Block Kit Builder](https://app.slack.com/block-kit-builder)에
붙여 넣으면 실제 Slack에서 어떻게 보이는지 미리 볼 수 있습니다.

## 3. 소스 추가·수정 — `config/sources.yaml`

모든 소스에는 `id`, `org`, `group`, `type`을 지정합니다. `include`/`exclude`(정규식)로 제목·요약을 필터링할 수 있습니다.

```yaml
# RSS / Atom (GitHub 릴리스 피드 github.com/<owner>/<repo>/releases.atom 도 가능)
- id: hf-blog
  org: Hugging Face
  group: bigtech
  type: rss
  url: https://huggingface.co/blog/feed.xml
  include: ['robot', 'lerobot']

# YouTube: channel_id(UC...) 또는 handle (youtube.com/@핸들)
- id: example-youtube
  org: Example Robotics
  group: humanoid
  type: youtube
  handle: "@example-handle"

# RSS가 없는 뉴스 목록 페이지: 글 링크의 경로 패턴(정규식)만 지정
#   글 주소가 https://example.com/news/some-post 형태라면:
- id: example-news
  org: Example Robotics
  group: humanoid
  type: webpage
  url: https://example.com/news
  link_pattern: '^/news/[^/?#]+/?$'

# GitHub 조직의 새 공개 저장소 (github.com/<org_name>)
- id: gh-example
  org: Example Robotics
  group: code
  type: github_org
  org_name: example-org

# Hugging Face 조직의 새 모델/데이터셋 (huggingface.co/<author>)
- id: hf-example
  org: Example Robotics
  group: code
  type: huggingface
  author: example-org
  kinds: [models, datasets]
```

추가한 뒤 `python -m digest --dry-run --no-llm --source <id> --backfill -v`로 항목이 잡히는지 먼저 확인하세요.

`id`는 중복 제거 기록의 키이므로 한 번 정한 뒤에는 바꾸지 마세요.
새로 추가한 소스는 첫 실행 때 자동으로 부트스트랩되어, 기존 글이 한꺼번에 쏟아지지 않습니다.

### arXiv watchlist 동작 방식
arXiv 메타데이터에는 소속 정보가 거의 없고, 단순 키워드 매칭은 오탐이 많습니다
("NVIDIA A100에서 학습", "OpenAI GPT-4o 사용", "DeepMind Control Suite" 등). 그래서 신호를 세 단계로 나눕니다.

- `strong`: 해당 조직의 연구라는 거의 확실한 신호입니다(프로젝트 페이지 도메인, "Physical Intelligence" 등). 바로 watchlist에 넣습니다.
- `weak`: 확인이 필요한 언급입니다("NVIDIA", "GR00T" 등). arXiv HTML 페이지의 **저자·소속 블록**에서 조직명을 찾으면 watchlist에 넣고, 없으면 후보로 남깁니다.
- `authors`: 지정한 저자가 포함된 논문은 바로 watchlist에 넣습니다. 연구실 관심 연구자를 자유롭게 추가하세요.

watchlist에 들지 않은 논문 중에서는 Claude가 `interest_profile` 기준으로 `llm_pick`편을 골라 추가합니다.

### 관심 분야 조정
`settings.interest_profile`을 수정하면 요약의 중요도 점수와 arXiv 논문 선정 기준이 함께 바뀝니다.

## 4. 비용

| 항목 | 예상 |
|---|---|
| Claude API (Sonnet 5.5) | 하루 입력 약 4만–8만 토큰 → 약 $0.1–0.3/일, **월 $3–10** |
| Claude API (Opus 5.5로 바꿀 때) | 약 2배 |
| GitHub Actions | 공개 저장소 무료 / 비공개는 무료 한도 내 |
| Slack | 무료 |

## 5. 한계와 참고 사항

- **X(트위터)**: API 비용 때문에 기본으로 제외했습니다. Tesla Optimus처럼 X에 먼저 올라오는 소식은
  YouTube 업로드나 공식 블로그를 통해 잡습니다.
- **JavaScript로 렌더링되는 사이트**: 목록 페이지에서 링크를 찾지 못하면 `sitemap.xml`으로 자동 전환합니다.
  둘 다 실패하면 다이제스트 하단의 "Sources that failed today"에 표시되니, 그때 `link_pattern`이나 `url`을 고치면 됩니다.
- **GitHub cron 지연**: GitHub의 예약 실행은 수 분에서 수십 분 늦게 시작될 수 있습니다.
  그래서 07:40에 시작해 08:00까지 기다렸다가 게시합니다. 08:00 이후에 시작되면 수집이 끝나는 즉시 게시합니다.
- **60일 비활성**: 공개 저장소는 60일 동안 활동이 없으면 예약 워크플로가 비활성화될 수 있습니다.
  이 봇은 매일 상태 파일을 커밋하므로 보통 문제가 없지만, 비활성화되면 Actions 탭에서 다시 켜 주세요.
- **요약 언어**: `settings.language`를 `Korean`으로 바꾸면 한국어로 요약합니다.

## 6. 문제 해결

| 증상 | 확인할 것 |
|---|---|
| 아무것도 안 옴 | 첫 실행은 부트스트랩이라 게시하지 않습니다. `backfill`로 한 번 실행해 보세요 |
| `Slack webhook error 404/403` | Webhook URL이 잘못됐거나 앱이 채널에서 제거됨 |
| 요약 없이 원문 발췌만 나옴 | `ANTHROPIC_API_KEY` 미설정 또는 결제·한도 문제. Actions 로그의 `summarization failed` 확인 |
| 특정 소스가 매일 실패 | `python -m digest --dry-run --no-llm --source <id> --backfill -v`로 로컬에서 확인 |
| 같은 글이 또 옴 | URL이 바뀐 경우입니다(추적 파라미터는 자동으로 제거). 해당 소스 `exclude`로 처리 |
| state 커밋 실패 | Workflow permissions가 *Read and write*인지 확인 |
