const SUMMARY = __SUMMARY__;
const TOTAL_INSP = __TOTAL__;
const DETAIL = __DETAIL__;
const GRAPH = __GRAPH__;
const STATUS_KO = __STATUS_KO__;
const STATUS_COLOR = __STATUS_COLOR__;

function showTab(name) {
  document.querySelectorAll('.tabpane').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.tabbtn').forEach(b => b.classList.remove('on'));
  document.getElementById('pane-' + name).style.display = 'block';
  document.getElementById('btn-' + name).classList.add('on');
}

const SUN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>';
const MOON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>';
function syncThemeBtn() {
  const dark = document.documentElement.dataset.theme !== 'light';
  const b = document.getElementById('themeBtn');
  if (b) { b.innerHTML = (dark ? SUN : MOON) + (dark ? '라이트' : '다크');
           b.setAttribute('aria-label', dark ? '밝은 화면으로 바꾸기' : '어두운 화면으로 바꾸기'); }
}

function toggleTheme() {
  const cur = document.documentElement.dataset.theme !== 'light';
  const next = cur ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('talog.theme', next); } catch (e) {}   // 콘솔과 같은 설정
  syncThemeBtn();
}

function fmtDur(s) {
  if (!s) return '-';
  if (s < 0) return '<span class="pill p-incomplete_lost" title="다존 로그 절단 경계 — 측정 불가">' + s.toFixed(2) + 's?</span>';
  return s.toFixed(2) + 's';
}

let STATUS_FILTER = 'all';
let SORT_DESC = true;      // true=최신순(기본), false=과거순
let PAGE_OFF = 0;          // 서버 모드 페이지 오프셋
// talog view(로컬 서버)로 열리면 검사 조회·간트 상세를 DB 쿼리로 전환한다
const SERVER_MODE = location.protocol.startsWith('http');
const API_BASE = window.TALOG_API || '';   // talog 콘솔은 /r/<리포트> 아래에서 서빙
const BAD_SET = new Set(['rejected', 'incomplete_lost', 'incomplete', 'unknown']);

function toggleSort() {
  SORT_DESC = !SORT_DESC;
  PAGE_OFF = 0;
  const b = document.getElementById('sortBtn');
  if (b) b.textContent = SORT_DESC ? '정렬: 최신순 ▾' : '정렬: 과거순 ▴';
  renderList();
}

function applyFilter(rows) {
  if (STATUS_FILTER === 'bad') return rows.filter(r => BAD_SET.has(r[4]));
  if (STATUS_FILTER === 'ng') return rows.filter(r => r[9] === 'NG');
  if (STATUS_FILTER === 'eof') return rows.filter(r => r[4] === 'in_progress_eof');
  if (STATUS_FILTER === 'ok') return rows.filter(r => r[4] === 'complete');
  return rows;
}

function filteredRows() {
  const f = (document.getElementById('insp-search').value || '').trim();
  let rows = SUMMARY;
  if (f) rows = rows.filter(r => r[0].includes(f) || (r[1] && r[1].includes(f)));
  return applyFilter(rows);
}

function setFilter(name) {
  STATUS_FILTER = name;
  PAGE_OFF = 0;
  document.querySelectorAll('.fbtn').forEach(b =>
    b.classList.toggle('on', b.dataset.f === name));
  renderList();
}

function movePage(delta) {
  PAGE_OFF = Math.max(0, PAGE_OFF + delta);
  renderList();
}

function renderRows(show, matched, extraNote) {
  const tb = document.getElementById('insp-body');
  const frag = [];
  for (const r of show) {
    const [iid, pid, ts, st, status, dur, ndone, nfed, ack, res] = r;
    const has = ' class="ilink mono" style="cursor:pointer;color:var(--blue)"';
    const resTxt = res === 'NG' ? '<span class="pill p-ng">NG</span>'
      : (res || '-');
    frag.push(`<tr><td>${st}</td><td${has} data-id="${iid}">${iid}</td><td>${pid || '-'}</td>` +
      `<td><span class="pill p-${status}">${STATUS_KO[status] || status}</span></td>` +
      `<td>${resTxt}</td>` +
      `<td class="r">${fmtDur(dur)}</td><td class="r">${ndone}/${nfed}</td></tr>`);
  }
  tb.innerHTML = frag.join('');
  document.getElementById('insp-count').textContent =
    `${matched.toLocaleString()}건 매칭 (표시 ${show.length}건${extraNote})`;
  const pg = document.getElementById('pageNav');
  if (pg) pg.style.display = 'inline';     // 내장/서버 모드 공통 페이지 이동
  bindLinks();
}

function timeRange() {
  const f = (document.getElementById('t-from') || {}).value || '';
  const t = (document.getElementById('t-to') || {}).value || '';
  return [f, t];
}

function renderList() {
  const [tf, tt] = timeRange();
  if (SERVER_MODE) {
    const q = encodeURIComponent(
      (document.getElementById('insp-search').value || '').trim());
    fetch(API_BASE + `/api/insp?filter=${STATUS_FILTER}&q=${q}` +
          `&sort=${SORT_DESC ? 'desc' : 'asc'}&offset=${PAGE_OFF}&limit=400` +
          `&from=${tf}&to=${tt}`)
      .then(r => r.json())
      .then(d => renderRows(d.rows, d.total,
        `, ${PAGE_OFF + 1}~${PAGE_OFF + d.rows.length} · DB 직접 조회`))
      .catch(() => renderRows([], 0, ' — 서버 응답 없음'));
    return;
  }
  let rows = filteredRows();
  // 기본(전체) 뷰에서는 '로그 절단(판정 불가)' 꼬리가 화면을 도배하지 않도록
  // 제외한다 — 절단 건은 '절단' 필터 버튼으로 조회
  if (STATUS_FILTER === 'all')
    rows = rows.filter(r => r[4] !== 'in_progress_eof');
  // 시간대 필터 (내장 모드): r[3] = "HH:MM:SS.fff"
  if (tf) rows = rows.filter(r => r[3].slice(0, 5) >= tf);
  if (tt) rows = rows.filter(r => r[3].slice(0, 5) <= tt);
  // 내장 모드도 페이지 이동 지원: 정렬 방향으로 늘어놓고 PAGE_OFF 슬라이스
  const ordered = SORT_DESC ? rows.slice().reverse() : rows;
  if (PAGE_OFF >= ordered.length) PAGE_OFF = Math.max(0, ordered.length - 400);
  const show = ordered.slice(PAGE_OFF, PAGE_OFF + 400);
  const capNote = ((typeof TOTAL_INSP !== 'undefined' && TOTAL_INSP > SUMMARY.length)
    ? ` · 전체 ${TOTAL_INSP.toLocaleString()}건 중 일부만 내장(6만 상한)` : '')
    + `, ${PAGE_OFF + 1}~${PAGE_OFF + show.length}`;
  renderRows(show, rows.length, capNote);
}

function exportCsv() {
  const head = ['inner_id', 'product_id', 'start', 'status', 'result',
                'duration_s', 'done', 'fed', 'ack'];
  const lines = [head.join(',')];
  for (const r of filteredRows()) {
    lines.push([r[0], r[1], r[3], STATUS_KO[r[4]] || r[4], r[9] || '',
                r[5], r[6], r[7], r[8] || ''].map(v =>
      `"${String(v).replace(/"/g, '""')}"`).join(','));
  }
  const blob = new Blob(['﻿' + lines.join('\r\n')],
                        {type: 'text/csv;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'talog_inspections.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}

function renderGantt(iid) {
  const d = DETAIL[iid];
  const box = document.getElementById('gantt-box');
  if (!d) {
    box.innerHTML = `<p>이 검사(${iid})의 채널 상세는 리포트에 내장되지 않았습니다. ` +
      `동봉된 .sqlite 에서 channel_runs 테이블을 조회하면 전체를 볼 수 있습니다.</p>`;
    return;
  }
  const runs = d.runs;
  if (!runs.length) {
    const why = d.status === 'rejected'
      ? '투입 전 거부된 검사입니다.'
      : (d.status === 'in_progress_eof'
        ? '로그 절단 구간이라 채널 기록이 잘렸습니다.'
        : '채널 실행 로그가 파싱되지 않았습니다 (alg 로그 포맷/레벨 미매칭 가능 — 진단 소견 참조).');
    box.innerHTML = `<h3><span class="mono">${iid}</span> — ${STATUS_KO[d.status] || d.status} (${d.st})</h3>` +
      `<p>${why}</p>`;
    return;
  }
  let maxT = 1;
  for (const r of runs) maxT = Math.max(maxT, r[3] + Math.max(r[4], 0.4));
  const W = 1060, LB = 300, rowH = 18;
  const H = runs.length * rowH + 40;
  const sx = t => LB + t / maxT * (W - LB - 20);
  const parts = [`<svg viewBox="0 0 ${W} ${H}" style="width:100%;background:var(--surface);border:1px solid var(--line);border-radius:12px">`];
  // 적응형 눈금: 검사 길이에 맞춰 5~8칸이 되도록 선택
  const nice = [0.2, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600];
  const gstep = nice.find(s => maxT / s <= 7) || 1200;
  const fmtT = t => '+' + (gstep < 1 ? t.toFixed(1) : Math.round(t)) + 's';
  for (let t = 0; t + gstep * 0.35 < maxT; t += gstep) {
    parts.push(`<line x1="${sx(t)}" y1="14" x2="${sx(t)}" y2="${H - 22}" style="stroke:var(--track)"/>` +
      `<text x="${sx(t)}" y="${H - 8}" font-size="10" text-anchor="middle" class="mut">${fmtT(t)}</text>`);
  }
  // 종료 시점 라벨 (검사 총 소요)
  parts.push(`<line x1="${sx(maxT)}" y1="14" x2="${sx(maxT)}" y2="${H - 22}" style="stroke:var(--grid)" stroke-dasharray="3 4"/>` +
    `<text x="${sx(maxT)}" y="${H - 8}" font-size="10" text-anchor="middle" style="fill:var(--ink2)" font-weight="600">+${maxT.toFixed(2)}s 끝</text>`);
  runs.forEach((r, i) => {
    const [alg, ch, ex, rel, dur, status, model, pre] = r;
    const y = 18 + i * rowH;
    const color = status === 'done' ? 'var(--blue)' : '#ec835a';
    const wpx = Math.max(3, sx(rel + Math.max(dur, 0.05)) - sx(rel));
    parts.push(`<text x="${LB - 6}" y="${y + 11}" font-size="11" text-anchor="end">` +
      `${alg}(${ch})${ex > 1 ? ' #' + ex : ''}</text>`);
    parts.push(`<rect x="${sx(rel)}" y="${y + 2}" width="${wpx}" height="12" rx="3" style="fill:${color}">` +
      `<title>${ch} exec${ex}\n시작 +${rel}s, 인퍼런스 ${dur ? dur + 's' : '(미완료)'}\n` +
      `전처리 ${pre}ms\n${model}\n상태: ${status}</title></rect>`);
    if (status !== 'done')
      parts.push(`<text x="${sx(rel) + wpx + 4}" y="${y + 12}" font-size="10" fill="#d03b3b">소실</text>`);
  });
  parts.push('</svg>');
  let head = `<h3><span class="mono">${iid}</span> — <span style="color:${STATUS_COLOR[d.status] || 'var(--ink)'}">` +
    `${STATUS_KO[d.status] || d.status}</span> <span class="hint">(시작 ${d.st} · ` +
    `채널 최종 종료까지 ${maxT.toFixed(2)}s · 막대에 마우스를 올리면 채널별 상세)</span></h3>`;
  if (d.lost && d.lost.length) head += `<p>소실 채널: <b>${d.lost.join(', ')}</b></p>`;
  if (d.nofeed && d.nofeed.length) head += `<p>미투입: ${d.nofeed.join(', ')}</p>`;
  if (d.remain) head += `<p>플랫폼 REMAIN 덤프: ${d.remain}</p>`;
  box.innerHTML = head + parts.join('');
}

function renderGraph(iid) {
  const box = document.getElementById('graph-box');
  if (!box || !GRAPH.algs.length) { if (box) box.innerHTML = '<p>그래프 데이터 없음</p>'; return; }
  let d = iid ? DETAIL[iid] : null;
  // 완료 검사인데 채널 런이 하나도 없으면(채널 로그 미파싱) 전 채널을
  // '이상 미투입' 빨강으로 칠하는 허위 표시를 피하고 중립(일자 요약)로 렌더
  const noRuns = d && d.status === 'complete' && !d.runs.length;
  if (noRuns) d = null;
  const doneSet = new Set(), execCnt = {};
  if (d) for (const r of d.runs) { if (r[5] === 'done') doneSet.add(r[0]); execCnt[r[0]] = (execCnt[r[0]] || 0) + 1; }
  const lostSet = new Set(d ? d.lostIdx : []), nofeedSet = new Set(d ? d.nofeedIdx : []),
        skipSet = new Set(d ? d.skipIdx : []);
  const RH = 19, top = 34;
  const nA = GRAPH.algs.length, nR = GRAPH.rois.length, nI = GRAPH.imgs.length;
  const H = top + Math.max(nA, nR, nI) * RH + 20;
  const yFor = (i, n) => top + i * RH + (Math.max(nA, nR, nI) - n) * RH / 2;
  const imgY = {}, roiY = {}, algY = {};
  GRAPH.imgs.forEach((m, i) => imgY[m.id] = yFor(i, nI));
  GRAPH.rois.forEach((r, i) => roiY[r.id] = yFor(i, nR));
  GRAPH.algs.forEach((a, i) => algY[a.id] = yFor(i, nA));
  // 이미지/ROI 데이터가 없으면 해당 컬럼을 접어 죽은 공간을 제거한다
  const hasImg = GRAPH.imgs.length > 0;
  const hasRoi = GRAPH.rois.length > 0;
  const roiX = hasImg ? 285 : 40;
  const algX = hasImg ? 565 : (hasRoi ? 320 : 40);
  const algW = Math.min(1075 - algX, 560);
  const P = [`<svg viewBox="0 0 1100 ${H}" style="width:100%;background:var(--surface)">`];
  if (hasImg)
    P.push(`<text x="90" y="18" font-size="12" class="mut" text-anchor="middle">이미지</text>`);
  if (hasRoi)
    P.push(`<text x="${roiX + 75}" y="18" font-size="12" class="mut" text-anchor="middle">ROI</text>`);
  P.push(`<text x="${algX + 205}" y="18" font-size="12" class="mut" text-anchor="middle">알고리즘 (검사 채널)</text>`);
  for (const r of GRAPH.rois) if (r.img && imgY[r.img] !== undefined)
    P.push(`<line x1="165" y1="${imgY[r.img] + 7}" x2="${roiX}" y2="${roiY[r.id] + 7}" style="stroke:var(--edge)" stroke-width="1"/>`);
  for (const a of GRAPH.algs) for (const ri of a.roi) if (roiY[ri] !== undefined) {
    let ec = 'var(--edge)', ew = 1;
    if (d) { if (lostSet.has(a.id)) { ec = '#ec835a'; ew = 1.5; }
             else if (nofeedSet.has(a.id)) { ec = '#d03b3b'; ew = 1.5; }
             else if (doneSet.has(a.id)) ec = 'var(--blue-soft)'; }
    P.push(`<line x1="${roiX + 150}" y1="${roiY[ri] + 7}" x2="${algX}" y2="${algY[a.id] + 7}" style="stroke:${ec}" stroke-width="${ew}"/>`);
  }
  for (const m of GRAPH.imgs)
    P.push(`<rect x="20" y="${imgY[m.id]}" width="145" height="15" rx="4" style="fill:var(--track);stroke:var(--grid)"/>` +
           `<text x="26" y="${imgY[m.id] + 11.5}" font-size="10.5">${m.label}</text>`);
  for (const r of GRAPH.rois)
    P.push(`<rect x="${roiX}" y="${roiY[r.id]}" width="150" height="15" rx="4" style="fill:var(--track);stroke:var(--grid)"/>` +
           `<text x="${roiX + 6}" y="${roiY[r.id] + 11.5}" font-size="10.5">${r.id} ${r.label}</text>`);
  for (const a of GRAPH.algs) {
    let fill = 'var(--surface)', stroke = 'var(--grid)', tip;
    if (d) {
      if (lostSet.has(a.id)) { fill = 'var(--tint-warn)'; stroke = '#ec835a'; tip = '실행 중 소실'; }
      else if (nofeedSet.has(a.id)) { fill = 'var(--tint-crit)'; stroke = '#d03b3b'; tip = '이상 미투입'; }
      else if (skipSet.has(a.id)) { fill = 'var(--track)'; stroke = 'var(--grid)'; tip = '정상 스킵(조건 비활성)'; }
      else if (doneSet.has(a.id)) { fill = 'var(--tint-ok)'; stroke = '#0ca30c'; tip = `완료 (실행 ${execCnt[a.id]})`; }
      else { tip = a.dl ? '기록 없음' : '비 DL 채널'; }
    } else {
      if (a.lost) { fill = 'var(--tint-warn)'; stroke = '#ec835a'; }
      else if (a.done) { fill = 'var(--tint-blue)'; stroke = 'var(--blue)'; }
      tip = `당일 완료 ${a.done}회` + (a.lost ? `, 소실 ${a.lost}회` : '');
    }
    P.push(`<rect x="${algX}" y="${algY[a.id]}" width="${algW}" height="15" rx="4" style="fill:${fill};stroke:${stroke}">` +
           `<title>alg${a.id} ${a.label} — ${tip}</title></rect>` +
           `<text x="${algX + 6}" y="${algY[a.id] + 11.5}" font-size="10.5">${a.id} ${a.label}</text>`);
  }
  P.push('</svg>');
  const mode = d ? `검사 <b>${iid}</b> 상태` : '일자 요약 (파랑=당일 실행, 주황=소실 발생 채널)';
  const legend = d ? ' · <span style="color:#0ca30c">■완료</span> <span style="color:#ec835a">■소실</span>' +
    ' <span style="color:#d03b3b">■이상 미투입</span> <span style="color:#898781">■정상 스킵</span>' : '';
  document.getElementById('graph-mode').innerHTML = mode + legend +
    (GRAPH.hasRecipe ? '' : ' · <span style="color:#898781">(레시피 없음 — 관측 기반 축약 그래프)</span>');
  box.innerHTML = `<div style="max-height:680px;overflow:auto;border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow)">${P.join('')}</div>`;
}

function gotoSection(id) {
  showTab('detail');
  const el = document.getElementById(id);
  if (el) setTimeout(() => el.scrollIntoView({behavior: 'smooth'}), 30);
}

function scrollMain(id) {
  showTab('main');
  const el = document.getElementById(id);
  if (el) setTimeout(() => el.scrollIntoView({behavior: 'smooth'}), 30);
}

function openInspection(iid) {
  gotoSection('sec-insp');
  document.getElementById('insp-search').value = iid;
  renderList(iid);
  if (!DETAIL[iid] && SERVER_MODE) {
    // 서버 모드: 어떤 검사든 간트 상세를 DB 에서 온디맨드로 불러온다
    fetch(API_BASE + '/api/detail?inner=' + encodeURIComponent(iid))
      .then(r => r.json())
      .then(d => {
        if (d && !d.error) DETAIL[iid] = d;
        renderGantt(iid);
        renderGraph(DETAIL[iid] ? iid : null);
      })
      .catch(() => { renderGantt(iid); renderGraph(null); });
    return;
  }
  renderGantt(iid);
  renderGraph(DETAIL[iid] ? iid : null);
}

function bindLinks() {
  document.querySelectorAll('.ilink, .tl').forEach(el => {
    el.onclick = ev => { ev.preventDefault(); openInspection(el.dataset.id); };
  });
}

function makeSortable() {
  document.querySelectorAll('table.sortable th').forEach((th, idx) => {
    th.style.cursor = 'pointer';
    th.title = '클릭하여 정렬';
    th.onclick = () => {
      const tb = th.closest('table').querySelector('tbody');
      const rows = [...tb.querySelectorAll('tr')];
      const asc = th.dataset.asc !== '1';
      th.dataset.asc = asc ? '1' : '0';
      rows.sort((a, b) => {
        const x = a.children[idx].innerText, y = b.children[idx].innerText;
        const nx = parseFloat(x.replace(/[^\d.-]/g, '')), ny = parseFloat(y.replace(/[^\d.-]/g, ''));
        const cmp = (!isNaN(nx) && !isNaN(ny)) ? nx - ny : x.localeCompare(y);
        return asc ? cmp : -cmp;
      });
      rows.forEach(r => tb.appendChild(r));
    };
  });
}

window.addEventListener('DOMContentLoaded', () => {
  showTab('main');
  syncThemeBtn();
  renderList('');
  renderGraph(null);
  document.getElementById('insp-search').addEventListener('input',
    () => { PAGE_OFF = 0; renderList(); });
  makeSortable();
  bindLinks();
});
