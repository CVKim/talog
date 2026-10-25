/* ── 도움말 · 용어 풀이 · 처음 안내 ───────────────────────────
   근거(UI/UX 가이드라인 ux-guidelines): #105 도움은 늘 같은 자리(위 막대의 ? 버튼, ? 키) · #116 용어 풀이는 마우스
   올림 전용이 아니라 누르는 버튼 · #88 처음 안내는 건너뛰기·이전이 있다 · #80 오류·빈 상태에는 다음 할 일.
   화면 글은 쉬운 말을 먼저 쓰고, 전문 용어에는 term("키") 풀이 버튼을 붙인다. 풀이 글은 여기 GLOSSARY 한 곳에만 둔다.
   dev: 1 인 용어, 세 번째 칸이 "dev" 인 안내 줄은 개발자 모드(Claude)에서만 보인다 (14-access.js). */

const GLOSSARY = {
  incident: {t: "사건", d: "비슷한 때 난 경보를 하나로 묶고, 원인 판단과 권고 조치를 붙인 기록입니다. 메일도 사건 단위로 갑니다."},
  alert: {t: "경보", d: "규칙 하나가 로그에서 이상을 본 순간입니다. 같은 원인의 경보 여러 개가 사건 하나로 묶입니다."},
  noinsp: {t: "NoInspThread (검사 시작 거부)", d: "검사 요청이 왔는데 놀고 있는 검사 스레드가 없어 talos 가 받지 못한 경우입니다. 설비는 그 제품을 미검사로 처리합니다."},
  seq_group: {t: "Seq 그룹", d: "검사를 맡는 스레드 묶음입니다(존·카메라 단위). 거부는 그룹마다 따로 나므로 talog 도 그룹마다 따로 봅니다."},
  idle: {t: "유휴 스레드", d: "검사 요청이 도착한 순간 그 그룹에서 놀고 있던 검사 스레드 수입니다. 0 이면 검사 시작 거부(NoInspThread)가 납니다."},
  hold: {t: "점유 시간", d: "검사 한 건이 스레드를 잡고 있는 시간입니다. 처리·결과 송신·저장까지 포함합니다."},
  interval: {t: "투입 간격", d: "같은 그룹에 검사 요청이 들어오는 간격(중앙값)입니다. 라인 속도가 바뀌면 달라집니다."},
  limit: {t: "한계", d: "스레드 수 × 투입 간격입니다. 검사 한 건의 점유 시간이 이보다 길어지면 스레드가 모자라 거부가 납니다."},
  tolerance: {t: "버틸 여유 (일시 지연 내성)", d: "한계에서 최근 60분 동안 가장 느린 검사(p99)의 점유 시간을 뺀 값을 한계 대비 %로 나타냅니다. 잠깐 느려져도 거부 없이 버틸 수 있는 여유입니다.",
    x: "40% 아래 주의 · 20% 아래 위험 · 0% 아래는 지금도 거부가 날 수 있는 상태"},
  baseline: {t: "기준선", d: "기동(재시작) 뒤 10분 워밍업을 지난 첫 1시간의 그룹별 값입니다. talog 는 이 '평소 상태'와 비교해 이상을 봅니다. 재시작하면 새로 잡습니다."},
  p99: {t: "p50 · p99", d: "p50 은 가운데 값, p99 는 100건 중 가장 느린 1건 수준입니다. 거부는 느린 쪽(p99)에서 납니다."},
  gate: {t: "판단 일치", d: "규칙 진단과 로컬 AI(LLM)가 같은 원인을 골랐다는 뜻입니다. 다르면 '판단 불일치'로 두 의견을 함께 보이고 사람이 확인합니다."},
  rule_diag: {t: "룰 진단", d: "근거 데이터를 런북의 판정 기준에 대어 원인을 정하는 규칙입니다. talog 의 결정은 늘 룰 진단입니다."},
  llm: {t: "LLM 2차 의견", d: "현장 PC 의 로컬 AI(Ollama)가 같은 근거를 읽고 낸 의견입니다. 결정하지 않고 교차 확인만 합니다. 인터넷으로 나가지 않습니다."},
  replay: {t: "경보 재현", d: "지난 로그를 지금 규칙으로 처음부터 다시 돌려, 그날 경보가 언제·몇 번 났을지 보는 것입니다. 메일은 보내지 않고, 결과에는 '재현' 표시가 붙습니다."},
  report: {t: "진단 리포트", d: "로그 폴더 하루치를 검사 한 건씩 정리한 HTML 보고서입니다. 미완료 원인·타임라인·처리 시간이 들어 있고, 질문하면 로컬 AI 가 리포트 DB 를 조회해 답합니다."},
  progressive: {t: "시간 경과형 처리 열화", d: "기동한 뒤 시간이 갈수록 검사 처리 시간이 조금씩 늘어나는 현상입니다. 계획 정지 때 재시작하면 기준으로 돌아옵니다."},
  masking: {t: "마스킹", dev: 1, d: "Claude 에 보내기 전에 검사 id 는 ID-xxxxxx 로, 경로는 <로그> 로 가립니다. 원래 값은 이 PC 에만 남습니다."},
  subscription: {t: "구독 사용량", dev: 1, d: "Claude 구독의 5시간·7일 사용 한도 중 쓴 비율입니다. 추가 과금은 없고, 한도에 가까우면 Sonnet·낮은 effort 를 권합니다."},
  effort: {t: "effort", dev: 1, d: "Claude 가 생각하는 깊이입니다. 높을수록 깊게 보지만 오래 걸리고 구독 사용량이 늘어납니다."},
  skill: {t: "작업 스킬", dev: 1, d: "Claude 에게 주는 작업 절차서입니다. 카드를 누르면 그 절차대로 talog 도구를 차례로 부르고 보고서를 남깁니다."},
  grounding: {t: "수치 근거 확인", dev: 1, d: "답에 적힌 숫자·시각이 그 대화에서 Claude 가 읽은 도구 결과에 실제로 있는지 talog 가 대조한 결과입니다. 없는 값은 계산했거나 지어낸 값일 수 있습니다."},
  tracking: {t: "추적 방식", d: "어떤 로그 파일을 실시간으로 읽을지입니다. '핵심 로그'가 기본이고 검사 PC 부하가 가장 적습니다."},
  brief: {t: "에이전트 브리핑", d: "talog 에이전트가 지난번 확인 뒤 생긴 일, 오늘 한 일, 처리 여유 전망, 마무리 안 된 사건을 문장으로 정리한 것입니다. 글은 모두 상태 기록으로 만들고 AI 가 지어낸 숫자는 없습니다."},
  health: {t: "설비 건강", d: "이미 쌓이는 기록으로 '곧 손볼 곳'을 점수 하나(100 = 이상 없음)로 보인 것입니다. 가장 낮은 항목이 점수입니다 — 처리 시간 흐름(기준선보다 꾸준히 느려지는지), 버틸 여유(지난 가장 큰 순간 튐을 버티는지), 판정 분포(NG 비율·결함 종류 비율이 평소와 다른지), 장비(GPU 온도·메모리), 마무리 안 된 사건.",
    x: "80 이상 좋음 · 50~79 주의 · 50 아래 점검 · 경보는 바꾸지 않고, 언제 한계에 닿을지는 예측하지 않습니다"},
  trace: {t: "처리 과정", d: "에이전트가 사건 하나를 처리한 순서입니다 — 경보 받음 → 사건으로 묶음 → 근거 모음 → 룰 진단 → 비슷한 과거 사례 → 로컬 AI 2차 의견 → 수치 대조 → 합의 관문 → 알림 → 사람의 확인."},
  resolve: {t: "사건 마무리 (해결 기록)", d: "사건을 해결(talog 판단 그대로 한 번 · 고쳐서 기록) · 오경보 · 확인 중으로 표시합니다. 잘못 눌렀으면 알림의 '되돌리기'. 해결 기록(실제 원인·한 조치)은 이 설비의 사례 KB 에 남아, 다음에 비슷한 사건이 나면 에이전트가 '비슷한 과거 사례'로 함께 보여 줍니다.",
    x: "오경보 표시는 설정 → 2 탐지의 규칙 피드백(최근 7일 경보·오경보 수)에 반영됩니다"},
  casekb: {t: "사례 KB", d: "과거 사건의 증상·원인·조치를 모은 기록입니다. 콘솔의 해결 기록과 개발자가 정리한 사례가 쌓이고, 에이전트가 새 사건과 비슷한 사례를 찾아 붙입니다. 이 PC 안에만 있습니다."},
  mode: {t: "모드 (현장 · 운영자 · 개발자)", d: "왼쪽 위에서 고릅니다. 현장 모드는 보기와 리포트, 운영자 모드는 여기에 설정 저장·시험·감시 정지·경보 재현, 개발자 모드는 여기에 Claude 분석을 더합니다.",
    x: "위 모드로 올릴 때만 PIN 을 묻습니다 · 고른 모드는 다음에 열 때도 그대로입니다"},
};

const HELP_VIEWS = {
  ops: {title: "운영 현황 읽는 법", intro: "위에서 아래로 세 덩어리입니다 — 지금, 오늘, 자세히. 보통은 맨 위 카드만 보면 됩니다.",
    steps: [["지금", "맨 위 한 줄이 지금 상태입니다(정상 · 확인할 사건 · 경보 · 감시 정지). 버튼 하나로 그 사건이나 경보로 갑니다. 그 아래 '할 일'에는 해야 할 것만 급한 순서로 나옵니다."],
      ["에이전트 브리핑", "지난번 확인 뒤 생긴 일 · 오늘 한 일 · 처리 여유 전망 · 마무리 안 된 사건을 문장으로 정리합니다. '접기'로 줄일 수 있습니다."],
      ["오늘", "오늘 생긴 사건과 경보입니다. 사건을 누르면 사건 화면에서 원인과 할 일을 봅니다."],
      ["설비 건강", "맨 위 카드 오른쪽 점수입니다. 급한 일이 없을 때 점수가 낮으면 그 이유가 제목으로 올라오고, '할 일'에 다음 정지 때 할 일이 나옵니다. 누르면 자세히 → 건강 탭."],
      ["자세히", "탭으로 모았습니다 — 건강(항목별 점수와 이유) · 처리 여유(그룹마다 잠깐의 지연을 버틸 여유) · 상태 기록(날짜 × 24시간) · 장비(GPU). 운영자 모드에는 시험 경보와 실행 로그도 있습니다."]],
    faq: [["그래프가 안 보여요", "맨 아래 '자세히' 카드의 탭에 있습니다. 고른 탭은 다음에 열 때도 그대로입니다."],
      ["숫자가 '—' 로 나와요", "아직 기록이 없거나 그룹별 기준선을 잡는 중입니다(기동 뒤 약 70분)."],
      ["'재현' 이 붙은 줄은 뭔가요", "분석 화면에서 지난 로그를 지금 규칙으로 다시 돌린 기록입니다. 오늘 실제 상태와는 다릅니다."]],
    terms: ["health", "brief", "tolerance", "limit", "baseline", "gate", "incident"]},
  inc: {title: "사건 화면 읽는 법", intro: "경보를 묶은 사건마다 원인 판단과 권고 조치를 모았습니다.",
    steps: [["사건 고르기", "왼쪽 목록에서 고릅니다. 줄마다 상태(새 사건 · 확인 중 · 해결 · 오경보)와 원인이 한 줄로 보입니다."],
      ["요약 세 줄", "무슨 일 · 원인 · 할 일만 읽으면 됩니다. 원인 옆 칩은 규칙과 로컬 AI 가 같은 원인을 골랐는지 알려 줍니다."],
      ["마무리", "다 봤으면 위쪽 버튼 한 번 — '해결 — 이대로'(talog 가 고른 원인·조치 그대로) / 고쳐서 기록(원인·조치를 고쳐 씀) / 오경보 / 확인만. 잘못 눌렀으면 아래 알림의 '되돌리기'. 해결 기록은 사례 KB 에 남아 다음 비슷한 사건에 함께 보입니다."],
      ["더 보기", "근거(판단 설명 · 핵심 사실 · AI 의견) · 경보 · 처리 과정(에이전트가 거친 10단계) · 메일 · 질문은 탭에 있습니다."],
      ["더 깊게", "'Claude 로 분석' 은 원본 로그까지 읽고 보고서를 남깁니다. 구독 로그인으로만 돌고 추가 비용이 없습니다.", "dev"]],
    faq: [["판단 불일치가 떴어요", "규칙과 로컬 AI 가 다른 원인을 골랐습니다. 근거 탭에서 두 의견을 비교하고 담당자가 확인하십시오."],
      ["재현 사건은 메일이 갔나요", "아니요. 지난 로그를 다시 돌려 만든 사건이라 메일을 보내지 않습니다."],
      ["오경보로 표시하면 무엇이 바뀌나요", "그 규칙의 오경보 수가 설정 → 2 탐지의 규칙 피드백에 쌓이고, 오경보가 많은 규칙에는 조건 조정을 권합니다. 규칙이 저절로 바뀌지는 않습니다."]],
    terms: ["incident", "trace", "resolve", "casekb", "alert", "gate", "rule_diag", "llm", "replay"]},
  ana: {title: "분석 화면 쓰는 법", intro: "로그 폴더 하나를 골라 진단 리포트·경보 재현을 합니다.", devIntro: "로그 폴더 하나를 골라 진단 리포트·경보 재현·Claude 조사를 합니다.",
    steps: [["① 폴더", "일자 폴더(…\\2026_10\\12), 설비 폴더, 여러 설비가 든 폴더 모두 됩니다. 최근 폴더는 칩으로 고릅니다."],
      ["② 할 일", "진단 리포트(검사별 판정·미완료 원인) · 경보 재현(지금 규칙으로 그날을 다시, 메일 없음) 중에서 고릅니다. 경보 재현은 운영자 모드에서 돌립니다."],
      ["Claude 조사", "'Claude 로 원본 로그 조사' 는 Claude 가 이 폴더의 원본 로그를 시각·문구·검사 id 로 직접 찾아 읽고 경과와 원인을 정리합니다.", "dev"],
      ["③ 결과", "진행 상황이 아래에 보이고, 끝나면 리포트 목록에 생깁니다. 재현 결과는 사건 화면의 '재현' 으로 봅니다."],
      ["리포트에 질문", "로컬 AI 가 리포트 DB 를 SQL 로 조회해 답하고, 답의 숫자를 조회 결과와 대조해 다른 값을 알려 줍니다."]],
    faq: [["분석하는 동안 감시는 느려지나요", "아니요. 분석은 낮은 우선순위의 별도 프로세스에서 돕니다."]],
    terms: ["report", "replay"]},
  ai: {title: "Claude 작업 공간 쓰는 법", intro: "Claude 가 talog 의 사건·리포트·원본 로그를 도구로 직접 읽고 답합니다.",
    steps: [["작업 카드", "카드를 누르면 필요한 값(사건·날짜·시각)만 묻고 그 작업의 절차(스킬)로 시작합니다."],
      ["확인한 과정", "Claude 가 부른 도구와 결과가 순서대로 보입니다. 펼치면 talog 가 돌려준 내용(가린 값 그대로)을 봅니다."],
      ["수치 근거", "답 아래 배지는 답의 숫자가 실제 도구 결과에 있는지 대조한 결과입니다. '없는 값' 이 있으면 보고서에서 확인하십시오."],
      ["보고서", "결과는 에이전트 폴더의 reports 에 HTML 로 남고, 답 아래 버튼으로 엽니다."]],
    faq: [["돈이 드나요", "아니요. 이 PC 에 로그인한 Claude 구독으로만 실행하고 API 키는 쓰지 않습니다. 구독 사용량에는 포함됩니다."],
      ["고객 데이터가 나가나요", "Claude 가 읽은 내용은 Anthropic 으로 갑니다. 검사 id·경로는 가려서 보내지만, 고객이 승인한 범위만 연결하십시오."],
      ["재현은 왜 확인을 받나요", "재현은 파일을 쓰고 수 분 걸리기 때문입니다. '규칙 바꾼 뒤 비교' 카드에서 켜고 한 번 더 확인해야 그 질문에만 허용됩니다."]],
    terms: ["skill", "grounding", "masking", "subscription", "effort"]},
  set: {title: "설정 화면 쓰는 법", intro: "talog.yaml 의 값을 단계별로 고칩니다. 저장하면 이전 파일을 .bak 으로 남기고 감시를 새 설정으로 다시 시작합니다.",
    steps: [["모드", "맨 위 카드에 지금 모드가 있습니다. 현장 모드에서는 보기만 됩니다 — 바꾸려면 운영자 모드로(왼쪽 위 또는 이 카드, PIN)."],
      ["① 수집", "설비 이름 · 로그 폴더 · 저장 폴더 세 칸만 맞추면 됩니다. 읽을 파일은 접혀 있고 보통은 '핵심 로그' 그대로 둡니다."],
      ["② 탐지", "경보 규칙 표는 접혀 있습니다. 요약 줄에 켠 규칙 수와 최근 경보·오경보가 보이고, 누르면 표가 펼쳐집니다(켜고 끄기 · 등급 · 'N분에 M번' 조건)."],
      ["③ 판단", "로컬 AI(LLM) 교차 확인을 켜고 모델을 고릅니다. 장치(CPU·GPU)와 속도 시험은 접어 두었습니다."],
      ["④ 알림", "메일 방식과 받는 사람을 정합니다. '연결 확인' 은 보내지 않고 로그인만 해 봅니다."]],
    faq: [["저장하지 않고 다른 화면으로 가도 되나요", "아래에 '저장하지 않은 변경' 막대가 남아 있어 다시 돌아와 저장하거나 되돌릴 수 있습니다."],
      ["입력 칸이 회색으로 잠겨 있어요", "현장 모드입니다. 왼쪽 위에서 운영자 모드로 바꾸면(PIN) 고칠 수 있습니다. 끝나면 현장 모드로 되돌려 두십시오."]],
    terms: ["mode", "tracking", "llm", "rule_diag"]},
};

/** 용어 풀이 버튼 HTML — 화면 글 옆에 붙인다(버튼 안에는 넣지 않는다). */
function term(key, label = "") {
  const g = GLOSSARY[key];
  if (!g) return "";
  return `<button type="button" class="term" data-term="${key}" aria-label="${esc((label || g.t) + " — 뜻 보기")}" aria-expanded="false" aria-controls="termPop">${ic("help", "xs")}</button>`;
}
/** 정적 화면 마크업의 <span data-tip="키"></span> 자리를 풀이 버튼으로 바꾼다. */
function hydrateTerms(root = document) {
  $$("[data-tip]", root).forEach(el => { el.outerHTML = term(el.dataset.tip, el.dataset.label || ""); });
}
hydrateTerms();

/* 용어 풀이 팝오버 — 버튼을 누르거나 Enter. Esc·바깥 누르기로 닫고, 닫으면 버튼으로 초점을 돌려준다 */
let termAnchor = null;
function placeTerm() {
  if (!termAnchor) return;
  const pop = $("#termPop"), r = termAnchor.getBoundingClientRect();
  const w = pop.offsetWidth, h = pop.offsetHeight;
  const x = Math.min(window.innerWidth - w - 12, Math.max(12, r.left + r.width / 2 - w / 2));
  let y = r.bottom + 8;
  if (y + h > window.innerHeight - 12) y = Math.max(12, r.top - h - 8);
  pop.style.left = `${x}px`;
  pop.style.top = `${y}px`;
}
function closeTerm(focusBack = false) {
  const b = termAnchor;
  termAnchor = null;
  $("#termPop").hidden = true;
  if (b) {
    b.setAttribute("aria-expanded", "false");
    if (focusBack) b.focus();
  }
}
function openTerm(btn) {
  if (termAnchor === btn) { closeTerm(); return; }
  closeTerm();
  const key = btn.dataset.term, g = GLOSSARY[key];
  if (!g) return;
  $("#termPopT").textContent = g.t;
  $("#termPopD").textContent = g.d;
  $("#termPopX").textContent = g.x || "";
  $("#termPopX").hidden = !g.x;
  $("#termPopMore").onclick = () => { closeTerm(); openHelp({tab: "terms", term: key}); };
  $("#termPop").hidden = false;
  termAnchor = btn;
  btn.setAttribute("aria-expanded", "true");
  placeTerm();
}
document.addEventListener("click", ev => {
  const b = ev.target.closest("[data-term]");
  if (b) { ev.preventDefault(); openTerm(b); return; }
  if (termAnchor && !ev.target.closest("#termPop")) closeTerm();
});
document.addEventListener("keydown", ev => { if (ev.key === "Escape" && termAnchor) { ev.preventDefault(); closeTerm(true); } });
window.addEventListener("scroll", placeTerm, {passive: true});
window.addEventListener("resize", placeTerm);

/* 도움말 서랍 — 이 화면 읽는 법 · 용어 사전 · 단축키. 위 막대의 ? 버튼과 ? 키, Ctrl+K 에서 연다 */
const HELP_TABS = [["view", "이 화면"], ["terms", "용어 사전"], ["keys", "단축키"]];
function helpTab(name) {
  $$("#helpDlg [role=tab]").forEach(b => {
    const on = b.dataset.tab === name;
    b.setAttribute("aria-selected", on);
    b.tabIndex = on ? 0 : -1;
    $("#" + b.getAttribute("aria-controls")).hidden = !on;
  });
  $("#helpDlg .drawer-b").scrollTop = 0;              // 탭마다 맨 위부터 (스크롤 칸을 함께 쓴다)
}
const forMode = rows => rows.filter(r => r[2] !== "dev" || isDev());     // ["제목", "글", "dev"] 는 개발자 모드에서만
function renderHelpView(v) {
  const H = HELP_VIEWS[v] || HELP_VIEWS.ops;
  $("#helpScreen").textContent = VIEWS[v] ? VIEWS[v][0] : "";
  $("#helpPnView").innerHTML = `<p class="help-intro">${esc(isDev() && H.devIntro || H.intro)}</p>
    <ol class="help-steps">${forMode(H.steps).map(([t, d]) => `<li><b>${esc(t)}</b><span>${esc(d)}</span></li>`).join("")}</ol>
    <h3 class="help-h">${ic("lightbulb", "sm")}자주 묻는 것</h3>
    <dl class="help-faq">${forMode(H.faq).map(([q, a]) => `<dt>${esc(q)}</dt><dd>${esc(a)}</dd>`).join("")}</dl>
    <h3 class="help-h">${ic("book", "sm")}이 화면의 용어</h3>
    <div class="help-chips">${H.terms.filter(k => !GLOSSARY[k].dev || isDev()).map(k => `<button type="button" class="chip" data-goterm="${k}">${esc(GLOSSARY[k].t)}</button>`).join("")}</div>`;
  $$("#helpPnView [data-goterm]").forEach(b => b.onclick = () => { helpTab("terms"); showTerm(b.dataset.goterm); });
}
function renderGlossary(q = "") {
  const s = q.trim().toLowerCase();
  const keys = Object.keys(GLOSSARY).filter(k => (!GLOSSARY[k].dev || isDev()) && (!s || `${GLOSSARY[k].t} ${GLOSSARY[k].d}`.toLowerCase().includes(s)));
  $("#helpTerms").innerHTML = keys.length
    ? keys.map(k => `<div class="gl" id="gl-${k}"><dt>${esc(GLOSSARY[k].t)}</dt><dd>${esc(GLOSSARY[k].d)}${GLOSSARY[k].x ? `<span class="gl-x">${esc(GLOSSARY[k].x)}</span>` : ""}</dd></div>`).join("")
    : emptyBox("맞는 용어가 없습니다", "다른 말로 찾아보십시오.", "search");
}
function showTerm(key) {
  $("#helpTermQ").value = "";
  renderGlossary();
  const el = $("#gl-" + key);
  if (!el) return;
  el.classList.add("on");
  // 서랍이 막 열리며 움직이는 중에는 부드러운 스크롤이 끊긴다 — 다음 그림 차례에 바로 옮긴다
  requestAnimationFrame(() => el.scrollIntoView({block: "center", behavior: "auto"}));
  setTimeout(() => el.classList.remove("on"), 2400);
}
function openHelp({tab = "view", term: key = ""} = {}) {
  closeTerm();
  renderHelpView(S.view);
  renderGlossary();
  helpTab(tab);
  const d = $("#helpDlg");
  if (!d.open) d.showModal();
  if (key) showTerm(key); else $("#helpDlg [aria-selected=true]").focus();
}
$("#helpTabs").innerHTML = HELP_TABS.map(([k, l], i) => `<button type="button" role="tab" id="htab-${k}" aria-controls="helpPn${k[0].toUpperCase() + k.slice(1)}" aria-selected="${i === 0}" tabindex="${i === 0 ? 0 : -1}" data-tab="${k}">${l}</button>`).join("");
$$("#helpTabs [role=tab]").forEach((t, i, all) => {
  t.onclick = () => helpTab(t.dataset.tab);
  t.onkeydown = e => {
    const k = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!k) return;
    e.preventDefault();
    const n = all[(i + k + all.length) % all.length];
    helpTab(n.dataset.tab);
    n.focus();
  };
});
$("#helpTermQ").addEventListener("input", ev => renderGlossary(ev.target.value));
$("#btnHelp").onclick = () => openHelp();
$("#helpX").onclick = () => $("#helpDlg").close();
$("#helpDlg").addEventListener("click", ev => { if (ev.target === $("#helpDlg")) $("#helpDlg").close(); });
$("#helpTour").onclick = () => { $("#helpDlg").close(); startTour(); };
document.addEventListener("keydown", ev => {
  const typing = ev.target.closest && ev.target.closest("input, textarea, select, [contenteditable=true]");
  if (ev.key === "?" && !typing && !ev.ctrlKey && !ev.metaKey && !document.querySelector("dialog[open]")) { ev.preventDefault(); openHelp(); }
});

/* 처음 안내 — 다섯 화면을 차례로 짚는다. 건너뛰기·이전이 있고, 끝내면 이 브라우저에서는 다시 띄우지 않는다 */
const TOUR = [
  {view: "ops", hl: "#opsHero", t: "talog 에 오신 것을 환영합니다", d: "이 설비의 검사 로그를 실시간으로 읽어 이상을 알리고, 원인과 할 일을 정리합니다. 30초면 둘러볼 수 있습니다."},
  {view: "ops", hl: "#opsHero", t: "맨 위 한 줄이 지금 상태입니다", d: "정상인지, 확인할 것이 있는지 한 줄로 알려 주고 버튼 하나로 그곳으로 갑니다. 해야 할 것이 있으면 그 아래 '할 일' 에 급한 순서로 나옵니다."},
  {view: "ops", hl: "#opsHero .hbadge", t: "설비 건강 — 곧 손볼 곳을 미리", d: "오른쪽 점수는 쌓인 기록으로 계산한 설비 건강입니다(100 = 이상 없음). 점수가 내려가면 이유와 다음 정지 때 할 일이 위에 나옵니다."},
  {view: "ops", hl: "#opsMore", t: "그래프와 기록은 '자세히' 에", d: "건강 · 처리 여유 · 상태 기록 · 장비를 탭으로 모았습니다. 고른 탭은 다음에도 그대로입니다. 모르는 말 옆 (?) 를 누르면 풀이가 나옵니다."},
  {view: "inc", hl: "#incDetail", t: "사건 — 무슨 일 · 원인 · 할 일", d: "사건을 고르면 세 줄 요약이 보입니다. 다 봤으면 '해결 — 이대로' 한 번이나 '오경보' 로 마무리하십시오 — 다음 비슷한 사건에 그 기록이 함께 보입니다."},
  {view: "ai", hl: "#caThread", t: "Claude — 깊은 분석", d: "작업 카드를 누르면 Claude 가 사건·리포트·원본 로그를 직접 읽고 보고서를 남깁니다. 구독 로그인으로만 돌고 추가 비용이 없습니다."},
  {view: "", hl: "#btnHelp", t: "도움말은 늘 오른쪽 위에", d: "어느 화면에서든 (?) 버튼이나 ? 키로 이 화면 읽는 법과 용어 사전을 엽니다. 이 안내도 거기서 다시 볼 수 있습니다."},
];
const TOUR_KEY = "talog.tour", TOUR_VER = "2.5";      // 화면이 크게 바뀐 판 — 예전 안내를 본 브라우저에도 한 번 다시
const tourSteps = () => TOUR.filter(s => s.view !== "ai" || isDev());   // 현장 모드에는 Claude 화면이 없다
let tourAt = 0, tourEl = null, tourTick = null;
function tourShow(i) {
  const T = tourSteps();
  tourAt = Math.max(0, Math.min(T.length - 1, i));
  const s = T[tourAt];
  if (s.view && S.view !== s.view) go(s.view, {focus: false});
  if (tourEl) tourEl.classList.remove("tour-hl");
  tourEl = $(s.hl);
  if (tourEl) {
    tourEl.classList.add("tour-hl");
    tourEl.scrollIntoView({block: "center", behavior: reduceMotion() ? "auto" : "smooth"});
  }
  $("#tourN").textContent = `${tourAt + 1} / ${T.length}`;
  $("#tourT").textContent = s.t;
  $("#tourD").textContent = s.d;
  $("#tourBack").disabled = tourAt === 0;
  $("#tourNext").lastChild.textContent = tourAt === T.length - 1 ? "시작하기" : "다음";
  $("#tourDots").innerHTML = T.map((_, k) => `<i class="${k === tourAt ? "on" : ""}"></i>`).join("");
}
function endTour() {
  clearInterval(tourTick);
  if (tourEl) tourEl.classList.remove("tour-hl");
  tourEl = null;
  $("#tourDlg").close();
  try { localStorage.setItem(TOUR_KEY, TOUR_VER); } catch (e) { /* 사생활 보호 모드 — 다음에 다시 보일 뿐 */ }
}
function startTour() {
  const d = $("#tourDlg");
  if (!d.open) d.show();
  // 화면은 몇 초마다 다시 그려진다(상태 카드의 class 도 새로) — 안내하는 동안 짚은 곳 표시를 되살린다
  clearInterval(tourTick);
  tourTick = setInterval(() => { if (tourEl && !tourEl.classList.contains("tour-hl")) tourEl.classList.add("tour-hl"); }, 400);
  tourShow(0);
  $("#tourNext").focus();
}
function maybeTour() {
  let done = "";
  try { done = localStorage.getItem(TOUR_KEY) || ""; } catch (e) { done = ""; }
  if (done !== TOUR_VER && !location.hash.startsWith("#inc/")) startTour();
}
$("#tourSkip").onclick = endTour;
$("#tourBack").onclick = () => tourShow(tourAt - 1);
$("#tourNext").onclick = () => { if (tourAt === tourSteps().length - 1) endTour(); else tourShow(tourAt + 1); };
$("#tourDlg").addEventListener("keydown", ev => { if (ev.key === "Escape") { ev.preventDefault(); endTour(); } });
