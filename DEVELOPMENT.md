# 개발 설명서

## 프로그램 흐름

1. `gui.py`가 설정을 읽고 PySide6(Qt Widgets) 화면을 생성합니다.
2. 사용자가 저장한 설정은 `config.json`에 기록됩니다.
3. `Monitor`가 주기별 작업을 별도 스레드에서 수행합니다.
4. `main.py`의 `State`가 `monitor_state.sqlite3`에 수집·전송 이력을 저장합니다.
5. Telegram API로 알림을 보낸 뒤 `notification_history`에 전송 내용을 기록합니다.

## 모듈별 역할

### `main.py`

- 게시판 목록과 개념글 목록 수집
- 게시글 제목, 본문, 댓글 수집
- 48시간 이내 게시글만 수집
- 최근 게시글 확인 수 1~200개
- 게시판 요청 간격 최소 1초
- 제목·본문·댓글 키워드 감지
- 본문·댓글 링크 감지
- `dclottery`, `youtube`, `youtu.be` 링크 제외
- 내장 욕설 사전과 문자 치환 규칙 검사
- 새 글·개념글·주제 요약·댓글 묶음·뉴비 알림
- 댓글 작성자도 뉴비로 판정되면 `👶` 아이콘 표시
- 댓글 작성자의 닉네임·식별코드·IP를 정규화해 작성자 이력 저장

### `gui.py`

- PySide6 기반 사이드바 대시보드, 알림 규칙, 텔레그램, SQLite 데이터 기록 탭
- `QTimer`와 로그 큐를 이용해 백그라운드 스레드의 로그를 안전하게 표시
- 모니터링 중 설정 변경은 다음 시작 때 반영되므로 중지 후 재시작 필요

- 게시판 주소와 모든 주기·묶음 개수 설정
- 제목·본문·댓글 키워드 입력
- 알림별 체크박스
- BotFather 안내 링크와 테스트 알림
- 설정 저장, DB 조회, 문서 열기
- 오른쪽 실행 로그 표시

### `database_viewer.py`

- PySide6 `QAbstractTableModel`로 SQLite 행을 표시하며 파일을 읽기 전용으로 엽니다.
- 앱 내 데이터 기록 탭 및 독립 실행 파일에서 함께 사용합니다.

SQLite의 주요 테이블을 조회합니다. 게시글·댓글 URL은 클릭하면 기본 웹 브라우저로 엽니다.

## 주요 설정

설정 키는 `main.py`의 `DEFAULT_CONFIG`에 정의되어 있습니다. 주요 항목은 다음과 같습니다.

- `board_url`: 모니터링 게시판 주소
- `topic_interval_minutes`: 1페이지 분석 알림 주기
- `keyword_interval_minutes`: 키워드·댓글·링크 검사 주기
- `new_title_interval_minutes`: 새 글 모아보기 주기
- `recent_posts_to_scan`: 검사할 최근 글 수, 1~200
- `request_gap_seconds`: 게시판 요청 간격, 최소 1초
- `title_keywords`, `body_keywords`, `comment_keywords`: 쉼표로 입력한 키워드 목록
- `new_title_batch_count`: 일반 새 글 모아보기 개수
- `newbie_title_batch_count`: 뉴비 새 글 모아보기 개수
- `concept_batch_count`: 개념글 누적 개수
- `comment_batch_count`: 댓글 묶음 개수
- `newbie_alert_enabled`: 작성자 뉴비 아이콘 표시 여부
- `link_alert_enabled`: 링크 포함 게시글·댓글 검사 여부
- `profanity_alert_enabled`: 욕설 댓글 검사 여부

## 중복 방지와 기준점

- 기존 `monitor_state.sqlite3`가 있으면 자동 삭제하거나 초기화하지 않습니다.
- DB가 없을 때만 최초 기준점을 생성하며 기존 글을 뉴비·새 글로 소급 알리지 않습니다.
- 제목·본문·댓글·링크·개념글은 기능별 식별자를 사용해 중복 전송을 막습니다.
- 본문은 성공적으로 확인한 글을 캐시하므로 같은 글을 반복해서 열지 않습니다.
- 댓글은 같은 게시글이어도 새 댓글이면 다시 확인합니다.
- 댓글 묶음 전송 완료 내역은 `comment_batch_pending`에서 제거됩니다.

## 뉴비 판정

최초 기준점은 최근 게시글 목록에서 확인 가능한 작성자 정보로 만들어집니다. 댓글 작성자는 댓글을 실제로 수집한 시점에 이력에 반영됩니다. 따라서 게시판의 전체 활동 이력을 의미하지 않으며, 최근 글 수·48시간 제한·요청 실패·식별코드 누락·유동 IP 변경에 따라 오판 가능성이 있습니다.

뉴비 새 글 모아보기와 뉴비 작성자 아이콘 표시는 별도 기능입니다. 댓글 작성자가 새로 판정되면 댓글 키워드·링크·욕설·댓글 묶음 알림에도 `👶`이 표시됩니다. DB에는 아이콘이 붙지 않은 원본 작성자 정보가 저장됩니다.

## 확장 시 주의할 부분

- HTML 선택자와 댓글 API 응답 구조는 디시인사이드 변경에 맞춰 수정해야 합니다.
- Telegram 토큰과 Chat ID는 소스에 하드코딩하지 말고 GUI 설정 또는 환경 변수로 관리해야 합니다.
- 새 알림 종류를 추가할 때는 알림 본문 생성, DB 중복 키, `notification_history` 기록을 함께 구현해야 합니다.
- 네트워크 요청을 추가할 때 `request_gap_seconds`를 지켜야 합니다.
