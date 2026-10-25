# herdr-courier

[![Tests](https://github.com/NaNIsaac/herdr-courier/actions/workflows/ci.yml/badge.svg)](https://github.com/NaNIsaac/herdr-courier/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

비공식 도구이며 herdr와 관계가 없습니다.

**[herdr](https://herdr.dev) pane용 프롬프트 큐: 사람이 입력하는 동안 에이전트 메시지는 기다립니다.**

[English](README.md)

herdr 워크스페이스의 에이전트끼리 `herdr agent prompt`로 메시지를 보내면, 그 글은 대상 입력
상자에 이미 있는 내용 뒤에 붙어서 제출됩니다. 그 pane에서 문장을 반쯤 쓰고 있었다면 쓰던 문장이
에이전트 메시지와 붙은 채 나갑니다. courier는 그 사이에 작은 데몬 `courier serve`를 둡니다. 에이전트와
스크립트는 `courier send`를 부르고, 데몬은 대상 입력 상자가 잠시 비어 있을 때만 메시지를
입력합니다. pane마다 큐가 따로 있어서 메시지는 순서대로 하나씩 들어갑니다.

```text
$ courier send boss "build finished, 3 tests failing" --wait
   (boss pane에서 입력 중: 메시지는 기다림)
   (내 메시지를 제출함: 상자가 다시 빔)
sent 4f1c2a9e07 to boss (w1:p2): agent working after submit
```

## 설치

[uv](https://docs.astral.sh/uv/)와 `PATH`에 있는 `herdr` CLI가 필요합니다. Windows와 Linux,
Python 3.13에서 테스트하며, Python은 필요하면 uv가 받아 옵니다.

<!-- test -->
```sh
uv tool install git+https://github.com/NaNIsaac/herdr-courier
uv tool update-shell
```

새 터미널을 열고 설치를 확인합니다.

<!-- test -->
```sh
courier --help
```

`courier`와, 같은 프로그램의 짧은 이름 `hc`가 설치됩니다(`hc send`, `hc serve`).
`hcd`는 `courier serve`의 세 번째 이름입니다. 업그레이드는 `uv tool upgrade herdr-courier`입니다.

## 처음 실행

데몬은 herdr의 셸 pane에서 띄웁니다. 그래야 에이전트 세션이 끝나도 살아 있습니다.

```sh
courier serve
```

그다음 같은 PC의 다른 pane에서:

```sh
courier doctor
courier send boss "[lane] status report"
```

`courier doctor`의 첫 줄에 `all checks pass`가 나와야 합니다. `boss` 자리에는 에이전트 이름이나
pane id를 씁니다. `herdr agent prompt`가 받는 대상과 같습니다.

## 예시

데몬을 띄웁니다. `hcd`와 `hc serve`는 `courier serve`의 다른 이름입니다.

```text
$ hcd
courier serve pid 13500 on 127.0.0.1:61710, transport agent, state C:\Users\you\AppData\Local\herdr-courier
```

레인에서 에이전트 `reviewer`에게 보고합니다.

```text
$ courier send reviewer "[lane] tests pass, PR opened"
queued 6f555376a5 for reviewer (w1:p61)
```

내가 그 pane에 입력하는 동안 데몬이 들어오지 못하게 막습니다. 그사이 보낸 메시지는 기다립니다.

```text
$ courier hold reviewer --reason "typing on the phone" --ttl 600
holding w1:p61 for 600 s
$ courier send reviewer "[lane] tests pass, PR opened"
queued 6f555376a5 for reviewer (w1:p61)
$ courier status
w1:p61  HOLD typing on the phone (lane, 599 s left)
  observed: idle, empty: dim placeholder 'Try "how does
    <filepath> work?"'
  6f555376a5 waiting      0s from lane: hold: typing on the
      phone
      [lane] tests pass, PR opened
$ courier release reviewer
released w1:p61
```

전체가 살아 있는지 확인합니다.

```text
$ courier doctor
courier doctor: all checks pass
ok   herdr      0.9.5; 13 panes, 6 agents
ok   dispatcher pid 13500, port 61710, api 1
ok   worker     last pass 0.0 s ago, no errors
ok   queue      0 queued, 0 held
ok   journal    empty (nothing queued since courier serve
                started)
ok   config     built-in defaults (no config.toml)
ok   writers    no other dispatcher running
ok   ovis       ok, revision d5a35b7, sees herdr 0.9.5;
                between turns only: secretary
```

pane 목록입니다(일부 행은 생략). `courier agents --json`은 같은 행을 JSON으로 출력합니다.

```text
$ courier agents
dispatcher: running, 0 queued, 0 held
  pane   tab               agent             status  queue
  w1:p1S boss              boss              working -
  ...
  w1:p61 reviewer          reviewer          idle    -
* = focused on the herdr server (any client)
shell = no agent in that pane
```

## 제약

- Claude Code의 입력 상자만 알아봅니다. courier가 상자를 읽지 못하는 pane(다른 에이전트, 대화상자,
  바뀐 화면 배치)으로 가는 메시지는 입력되지 않고 큐에 남습니다. `courier cancel <id>`로 지웁니다.
- 큐에 든 메시지에는 만료가 없습니다. 상자가 비고 pane이 hold 상태가 아닐 때까지, 또는 취소할
  때까지 기다립니다.
- `courier send`로 보낸 메시지만 큐에 들어갑니다. `herdr agent prompt`를 직접 부르는 프로그램은
  여전히 입력 중인 글 위에 쓸 수 있습니다.
- 상태 폴더 하나에 `courier serve` 하나, PC 한 대 안에서만 동작합니다. 127.0.0.1에서만 받습니다.
- Ovis 채팅에서 대기 중인 메시지는 courier가 볼 수 없습니다. Ovis가 다른 프로그램에 알려 주지 않기
  때문입니다. 대신 `ovis.panes`의 에이전트에게는 턴이 끝날 때까지 기다렸다가 보내므로, 턴이 길면 그만큼
  늦게 갑니다.

## 쓰는 법

### 한 에이전트가 다른 에이전트에게 보고

```sh
courier send boss "[lane] tests pass, PR opened"
courier send boss "[lane] need a decision" --wait --timeout 120000
```

`--wait`가 없으면 `send`는 큐에 넣자마자 돌아옵니다. 있으면 전달될 때까지, 그리고
`herdr agent prompt --wait`처럼 에이전트가 응답을 마칠 때까지 기다립니다.

### 폰에서 긴 메시지 입력

```sh
courier hold boss --reason "typing on the phone" --ttl 600
courier release boss
```

hold 중인 pane에는 오래 멈춰 있어도 아무것도 입력되지 않습니다. `--ttl`을 주면 저절로 풀리고,
없으면 `courier release`까지 유지됩니다. 데몬을 다시 띄워도 유지됩니다.

### 누가 어디 있는지 보기

```sh
courier agents
courier panes
```

`agents`는 모든 pane을 탭, 에이전트 이름, 상태, 큐와 함께 보여 줍니다. herdr의 폰 화면은 탭
라벨만 보여 주므로, 탭 이름이 그 안의 에이전트 이름과 다르면 경고합니다. `panes`는 pane을 탭별로
묶어 라벨과 작업 폴더를 보여 주고, 라벨이 [설정](#설정)의 이름 규칙을 어기면 경고합니다. 둘 다
62열 폰 터미널에 맞습니다.

터미널에서 `agents`는 상태에 색을 입히고(working 초록, idle 회색, done 파랑, blocked 빨강),
에이전트 이름은 굵게, 대기 중인 큐는 노랑, shell pane은 흐리게 보여 줍니다. `NO_COLOR`나
`--color=never`로 끄고, 출력이 터미널이 아닐 때도 색을 원하면 `--color`를 붙입니다. Windows의
herdr pane도 터미널로 보이지 않으므로 거기서는 `--color`가 필요합니다.

### 모두 살아 있는지 확인

```sh
courier doctor
courier status
```

`doctor`는 아무것도 바꾸지 않습니다. herdr, 데몬과 worker, 10분 넘게 기다리는 메시지, 만료 없는
hold, 지난 한 시간의 실패, journal, 설정 파일, pane에 입력하는 다른 프로그램, Ovis를 확인하고
문제마다 고치는 방법을 제시합니다. 실패한 검사가 있으면 1로 끝납니다. `status`는 모든 큐와 hold,
그리고 데몬이 각 pane에서 마지막으로 본 것을 보여 줍니다.

## 레퍼런스

### 명령

| 명령 | 하는 일 |
| --- | --- |
| `courier serve [--port N] [--tick S]` | 데몬을 포그라운드로 실행 (port 기본값 0은 빈 포트를 고름); `hc serve`, `hcd`도 같음 |
| `courier send TARGET TEXT [--wait] [--until STATUS] [--timeout MS] [--source NAME] [--human]` | 메시지를 큐에 넣음. `--human`은 큐의 에이전트 메시지보다 앞에 넣음 |
| `courier status [TARGET] [--recent N] [--json] [--width N]` | pane별 큐, hold, 마지막 관찰 |
| `courier hold TARGET [--ttl SECONDS] [--reason TEXT] [--owner NAME]` | 데몬이 그 pane에 입력하지 못하게 함 |
| `courier release TARGET` | hold 해제 |
| `courier cancel ID` | 큐의 메시지 삭제 |
| `courier agents [--wide] [--json] [--width N] [--color[=WHEN]]` | pane별 탭, 에이전트, 상태, 큐 |
| `courier panes [--json] [--width N]` | 탭별 pane과 라벨, 작업 폴더 |
| `courier doctor [--json] [--width N]` | 읽기 전용 상태 점검 |

`--width` 기본값은 터미널 폭이고, 알 수 없으면 62입니다. `--source`와 `--owner` 기본값은
`COURIER_SOURCE`, 없으면 부른 pane의 `HERDR_PANE_ID`입니다.

### 종료 코드

| 코드 | 뜻 |
| --- | --- |
| 0 | 완료. `send --wait`가 전달한 뒤에는 `herdr agent wait`의 종료 코드 |
| 1 | 실패: 전달 실패, 요청 거부, herdr 무응답, 잘못된 설정, doctor 검사 실패 |
| 3 | 데몬이 돌고 있지 않음. 아무것도 보내지 않았음 |
| 4 | `send --wait --timeout` 시간 초과 |

### 데몬이 돌고 있지 않을 때

`courier send`는 3으로 끝나며 손으로 전달하는 방법을 출력합니다.

```sh
herdr agent read boss --source recent --lines 12 --format ansi
# in the last input box, the text after the prompt glyph must be empty or dim
herdr agent prompt boss "[lane] status report"
```

### 파일

상태 폴더는 Windows에서 `%LOCALAPPDATA%\herdr-courier`, 그 밖에서는
`$XDG_STATE_HOME/herdr-courier`(`~/.local/state/herdr-courier`)이며 `COURIER_STATE_DIR`로 옮길 수
있습니다. 사용자의 `config.toml`과 데몬의 `journal.jsonl`, `lock`, `endpoint.json`(포트와 접근
토큰)이 들어 있습니다. 0.1의 상태 폴더(`herdr-console`)는 데몬이 처음 뜰 때 새 이름으로 옮깁니다.

### 설정

모든 설정에는 내장 기본값이 있습니다. 바꾸려면 [config.example.toml](config.example.toml)을 상태
폴더의 `config.toml`로 복사하고 바꿀 키만 남깁니다. 모르는 키나 잘못된 타입은 오류이며
`courier doctor`가 알려 줍니다.

| 키 | 기본값 | |
| --- | --- | --- |
| `panes.separator` | `" · "` | pane 라벨은 `<접두어> · <이름>` |
| `panes.worker_separator` | `" › "` | 다른 pane이 띄운 worker는 `<탭> › <작업>` |
| `panes.named_after_tab` | `["lane", "core", "isaac"]` | 이름이 탭 라벨과 같아야 하는 접두어 |
| `panes.named_freely` | `["infra"]` | 아무 이름이나 되는 접두어. 두 목록이 모두 `[]`이면 검사하지 않음 |
| `panes.path_variables` | `["DEV", "DATA"]` | 이 변수 아래의 작업 폴더는 `$DEV\...`로 표시 |
| `doctor.other_writers` | `["~/.isaac-control-center/dispatch/endpoint.json"]` | pane에 입력하는 다른 프로그램의 endpoint 파일 |
| `ovis.url` | `"http://127.0.0.1:7317"` | Ovis 상태 경로. `""`이면 Ovis 없음 |
| `ovis.panes` | `["secretary"]` | Ovis가 응답하는 동안 이 에이전트에게 가는 메시지는 턴 사이에만 보냄 |
| `ovis.settle` | `3.0` | 턴이 끝난 뒤 에이전트 상태가 이 초 동안 그대로여야 그런 메시지를 보냄 |

## 동작 방식

전달 조건, 전달 상태, 데몬의 보안 모델은 [docs/DISPATCH.md](docs/DISPATCH.md)에 있습니다.

## 개발

clone한 폴더에서:

<!-- test -->
```sh
uv sync
uv run pytest -q
```

## 라이선스

[MIT](LICENSE)
