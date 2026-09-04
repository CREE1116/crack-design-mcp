# Crack Design MCP (`crack-design-mcp`)

Crack 스토리 챗 창작·검증·실시간 시뮬레이션 통합 하네스 & MCP 서버입니다.

## 1. 핵심 철학
- **창작은 모델(LLM)이, 양식과 기계적 검증은 로직(MCP)이**:
  - 모델은 세계관, 갈등, 캐릭터 대사와 감정선을 창작합니다.
  - 로직(MCP)은 표준 뼈대 양식 제공, JSON 기반 키워드북 일괄 삽입/수정, UTF-16 단위 글자 수 측정, 미정의 기호 감사, 3슬롯 충돌 검사를 기계적으로 처리합니다.
- **제한 초과 시 차단이 아닌 압축 알림 (Save & Alert)**:
  - 400자(키워드북), 7,000자(통합 프롬프트) 초과 시 저장을 거부하지 않고 일단 안전하게 저장한 뒤, 초과 글자 수와 압축 가이드를 모델에게 명확히 전달합니다.
- **시뮬레이션 시 페르소나 완전 분리 (Prevent Omniscience)**:
  - 모델이 모든 설정을 다 알고 있어서 생기는 지식 누출을 방지하기 위해, `get_tester_context`를 통해 **플레이어 페르소나(순수 무지 상태)**와 **서술자 페르소나(활성화된 3개 키워드만 주입된 상태)**를 엄격히 분리하여 테스트할 수 있습니다.

## 2. 주요 MCP 도구 (총 48종)

### 템플릿 & 창작 지원
- `get_template`: 메인 프롬프트 8대 섹션 표준 뼈대, 캐릭터 3분리 템플릿, 프롤로그, 오프닝 양식 조회
- `get_design_guide`: 인물 설계 3분리 원칙, 7,000자 섹션 구조, 3슬롯 키워드북 규칙, 오프닝 설계 가이드

### 프롬프트 & 키워드북 관리
- `get_prompt` / `update_prompt`: 통합 프롬프트 및 프롤로그/오프닝 조회 및 수정 (UTF-16 글자수 측정 및 압축 알림)
- `list_keyword_entries`: 키워드북 전체 항목을 JSON 구조로 조회
- `get_keyword_entry`: 특정 키워드북 항목 상세 조회
- `update_keyword_entry`: 단일 키워드북 항목 추가/수정 (불릿 배열 지원, 400자 및 1~5개 키워드 검증)
- `batch_import_keywords`: JSON 배열로 여러 키워드북 항목을 일괄 등록 (Markdown 자동 변환 및 압축 알림)
- `delete_keyword_entry`: 키워드북 항목 삭제
- `reload_project`: 변경 사항 메모리 즉시 재반영

### 종합 감사
- `audit_project`: 프로젝트 전체(글자 수 한도, SAFE/UNSAFE 대칭성, 미정의 기호/이모지, 폐기된 네임스페이스, 3슬롯 충돌) 종합 진단

### 시뮬레이션 & 페르소나 격리
- `get_tester_context`: 플레이어 페르소나(순수 무지) vs 서술자 페르소나(3개 키워드 격리) 전용 프롬프트 제공
- `start_session`: 플레이 세션 생성
- `play_turn`: 턴 진행 (reply 모드 또는 외부 모델) 및 출력 계약(Info HUD 등) 검증
- `inspect_prompt`: 실제로 모델에 주입되는 프롬프트 및 3슬롯 활성화/드롭 확인
- `activation_report`: 여러 턴의 키워드 상시발동/미발동/슬롯초과 통계 분석
- `list_sessions` / `get_session` / `delete_session`: 세션 관리
- `list_start_sets` / `use_start_set`: 멀티 오프닝 시작 세트 관리

## 3. 실행 방법

```bash
./crack-mcp.sh up        # 서버 + 터널 기동, MCP URL 출력
./crack-mcp.sh restart   # 서버만 재기동 — 터널 URL은 그대로 유지
./crack-mcp.sh status    # 서버·터널·health 상태
./crack-mcp.sh url       # 현재 MCP URL만 출력
./crack-mcp.sh down      # 둘 다 정지
```

`trycloudflare` 퀵터널은 cloudflared 가 재시작될 때마다 새 호스트명을 발급합니다. 클라이언트가 설정해 둔 주소가 깨지지 않도록 서버와 터널의 수명을 분리했으므로, 코드를 고친 뒤에는 `restart` 를 쓰십시오 — `up`/`down` 만 터널을 건드립니다.

### 환경 변수
| 변수 | 기본값 | 용도 |
|---|---|---|
| `CRACK_WORKSPACE` | `~/crack` | 스토리 프로젝트들이 놓인 상위 디렉터리 |
| `CRACK_STATE` | `~/.crack-emu` | 세션·로그·`crack.db`·export 저장 위치 |
| `CRACK_PROJECT` | `$CRACK_WORKSPACE/마왕성주식회사/build` | 기본으로 열 빌드 디렉터리 |
| `CRACK_SYNC_TOOL` | `$CRACK_WORKSPACE/crack-story-chat-skill/tools/sync/crack_sync.py` | 크랙 에디터 동기화 스크립트 |
| `CRACK_MCP_PORT` | `8787` | MCP/웹 UI 포트 |
| `CRACK_MCP_AUTH_TOKEN` | (없음) | 설정 시 HTTP MCP 인증 토큰 |

## 4. 충실도(fidelity) 원칙

`crack_design/spec/crack_spec.yaml` 이 크랙 런타임 동작의 단일 출처이며, 모든 항목에 근거 등급이 달려 있습니다.

- `[OBSERVED]` — 추출된 프롬프트 덤프에 실제로 있는 것 (출처: dcinside 뤼튼 마갤 #961451)
- `[UNVERIFIED]` — 출처에 서술이 없는 합리적 추정. 실측되면 교체
- `[EXTENSION]` — 크랙에 있는지 불명한 자체 확장. `fidelity: crack` 에서는 기본 비활성

**근거 없는 규칙을 코드에 하드코딩하지 않는 것**이 이 프로젝트의 규율입니다. 예를 들어 3슬롯 초과 시 무엇을 남기는지는 원문에 서술이 없으므로, 기본값은 문서 순서(`keyword.priority: doc_order`)이고 "이번 입력 우선"은 `input_first` 확장으로 분리돼 있습니다. 벡터 임베딩 회상도 같은 이유로 미구현입니다 — 넣는 순간 QA 대상이 크랙이 아니게 됩니다.
