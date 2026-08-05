/* ============================================================
   KRX 백테스터 - 런처 (HTA / mshta.exe, IE11 엔진)

   ES5 문법만 쓴다.
   ES6 이후 문법(화살표함수, 새 변수 선언 키워드, 템플릿 문자열,
   비동기 결과 객체, 최신 HTTP 함수, 새 객체 선언 키워드)은
   IE 엔진에서 그대로 죽는다. 쓰지 마라.

   외부 세계(파일, 프로세스, HTTP)와 닿는 부분은 전부 SYS 하나로 모았다.
   테스트에서는 window.__MOCK_SYS 를 미리 넣어 두면 그것을 대신 쓴다.
   ============================================================ */

/* eslint-disable no-var */
(function () {

var DEF_PORT = 8000;
var PORT_TRIES = 10;       /* 포트가 막혔을 때 위로 몇 개까지 찾아볼지 */

/* 포트는 launcher_config.json 으로 바뀔 수 있어 상수로 두지 않는다 */
function baseUrl() { return "http://127.0.0.1:" + S.port + "/"; }
function statusUrl() { return baseUrl() + "api/status"; }

var STEP_MIN = 320;        /* 단계 하나가 화면에 머무는 최소 시간 */
var DONE_HOLD = 1400;      /* 완료 화면을 보여 주는 최소 시간 */
var CLOSE_MIN = 18 * 60 + 30;  /* 국내 시세가 올라오는 시각 (18:30) */

var S = {
  root: "",
  port: 8000,
  cfg: null,
  portInfo: null,
  logMark: 0,
  force: false,
  cancelled: false,
  finished: false,
  timers: [],
  childPid: 0,
  serverPid: 0,
  openedBrowser: false,
  lastSync: "",
  apiLatestTradeDate: "",
  logShown: ["", "", ""],
  tally: { prog: 0, progCommits: 0, quoteDays: 0, quoteCloned: false, server: false, fetched: false }
};

var P = {};

/* ============================================================
   0. 잡 도구
   ============================================================ */

function trim(s) {
  if (s === null || typeof s === "undefined") { return ""; }
  return String(s).replace(/^[\s﻿ ]+|[\s﻿ ]+$/g, "");
}

function pad2(n) { return (n < 10 ? "0" : "") + n; }

function nowDate() {
  if (typeof window.__MOCK_NOW !== "undefined" && window.__MOCK_NOW) {
    return new Date(window.__MOCK_NOW.getTime());
  }
  return new Date();
}

function fmtDate(d) {
  return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
}

/* "2026-08-04 20:11:03" 또는 "2026-08-04" 를 Date 로. 실패하면 null */
function parseStamp(s) {
  s = trim(s);
  var m = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?/.exec(s);
  if (!m) { return null; }
  return new Date(
    parseInt(m[1], 10), parseInt(m[2], 10) - 1, parseInt(m[3], 10),
    m[4] ? parseInt(m[4], 10) : 0,
    m[5] ? parseInt(m[5], 10) : 0,
    m[6] ? parseInt(m[6], 10) : 0
  );
}

function el(id) { return document.getElementById(id); }

function later(fn, ms) {
  var t = setTimeout(function () { if (!S.cancelled) { fn(); } }, ms);
  S.timers[S.timers.length] = t;
  return t;
}

function clearTimers() {
  for (var i = 0; i < S.timers.length; i++) {
    try { clearTimeout(S.timers[i]); } catch (e) {}
  }
  S.timers = [];
}

/* ============================================================
   1. SYS - 바깥 세계와 닿는 유일한 창구
   ============================================================ */

function makeRealSys() {
  var fso = new ActiveXObject("Scripting.FileSystemObject");
  var sh = new ActiveXObject("WScript.Shell");

  function readViaStream(path) {
    try {
      var st = new ActiveXObject("ADODB.Stream");
      st.Type = 2;
      st.Charset = "utf-8";
      st.Open();
      st.LoadFromFile(path);
      var s = st.ReadText(-1);
      st.Close();
      if (s && s.charAt(0) === "﻿") { s = s.substring(1); }
      return s;
    } catch (e) { return null; }
  }

  function readViaFso(path) {
    try {
      if (!fso.FileExists(path)) { return null; }
      var f = fso.OpenTextFile(path, 1, false, 0);
      var s = f.AtEndOfStream ? "" : f.ReadAll();
      f.Close();
      return s;
    } catch (e) { return null; }
  }

  return {
    exists: function (p) { try { return !!fso.FileExists(p); } catch (e) { return false; } },
    dirExists: function (p) { try { return !!fso.FolderExists(p); } catch (e) { return false; } },
    mkdir: function (p) { try { if (!fso.FolderExists(p)) { fso.CreateFolder(p); } } catch (e) {} },
    del: function (p) { try { if (fso.FileExists(p)) { fso.DeleteFile(p, true); } } catch (e) {} },

    /* UTF-8 로 먼저 읽고, 깨지면 시스템 코드페이지(CP949)로 다시 읽는다.
       git 출력은 UTF-8, 배치 echo 출력은 CP949 라 둘 다 나올 수 있다. */
    read: function (p) {
      var t = readViaStream(p);
      if (t === null || t.indexOf("�") >= 0) {
        var a = readViaFso(p);
        if (a !== null) { return a; }
      }
      return t === null ? "" : t;
    },

    /* ansi = true 면 CP949 로 쓴다 (.bat 은 반드시 ansi) */
    write: function (p, text, ansi) {
      try {
        var f = fso.CreateTextFile(p, true, ansi ? false : true);
        f.Write(text);
        f.Close();
        return true;
      } catch (e) { return false; }
    },

    /* 콘솔 창 없이 띄우고 PID 를 돌려준다 (기존 VBS 의 ShowWindow=0 방식) */
    spawn: function (cmdline, cwd) {
      try {
        var svc = GetObject("winmgmts:{impersonationLevel=impersonate}!\\\\.\\root\\cimv2");
        var startup = svc.Get("Win32_ProcessStartup").SpawnInstance_();
        startup.ShowWindow = 0;
        var inp = svc.Get("Win32_Process").Methods_("Create").InParameters.SpawnInstance_();
        inp.CommandLine = cmdline;
        inp.CurrentDirectory = cwd;
        inp.ProcessStartupInformation = startup;
        var outp = svc.ExecMethod_("Win32_Process", "Create", inp);
        if (outp && Number(outp.ReturnValue) === 0) { return Number(outp.ProcessId); }
      } catch (e) {}
      /* WMI 가 막힌 환경이면 창 없이 띄우기만 하고 PID 는 포기한다 */
      try { sh.Run(cmdline, 0, false); } catch (e2) {}
      return 0;
    },

    kill: function (pid) {
      if (!pid) { return; }
      try { sh.Run("taskkill /PID " + pid + " /T /F", 0, false); } catch (e) {}
    },

    /* 이 폴더의 .venv 파이썬만 골라서 정리한다 (다른 파이썬은 손대지 않는다) */
    killVenvPython: function (venvPy) {
      try {
        var target = String(venvPy).toLowerCase();
        var svc = GetObject("winmgmts:\\\\.\\root\\cimv2");
        var list = svc.ExecQuery(
          "SELECT ProcessId, ExecutablePath FROM Win32_Process " +
          "WHERE Name = 'python.exe' OR Name = 'pythonw.exe'");
        var e = new Enumerator(list);
        for (; !e.atEnd(); e.moveNext()) {
          var p = e.item();
          var ep = "";
          try { ep = String(p.ExecutablePath || "").toLowerCase(); } catch (e1) { ep = ""; }
          if (ep === target) { try { p.Terminate(); } catch (e2) {} }
        }
      } catch (e3) {}
    },

    open: function (url) { try { sh.Run(url, 1, false); } catch (e) {} },

    /* localhost 프로브. 두 가지 함정이 있어 둘 다 막는다.
       (1) ServerXMLHTTP 는 WinHTTP 프록시 설정을 따른다. 회사 PC 처럼 프록시가
           잡혀 있으면 127.0.0.1 요청까지 프록시로 나가려다 실패해서, 서버가
           멀쩡히 떠 있어도 "없다"로 오판한다.
           -> setProxy(1) = SXH_PROXY_SET_DIRECT 로 프록시를 우회한다.
       (2) 서버가 긴 백테스트를 도는 중이면 응답이 늦다. 짧은 타임아웃으로
           끊으면 역시 "없다"로 오판한다. -> 6초까지 기다린다. */
    ping: function (cb) {
      var http = null, fired = false, wd = null;
      function fin(ok, body) {
        if (fired) { return; }
        fired = true;
        if (wd) { try { clearTimeout(wd); } catch (e0) {} wd = null; }
        try { if (http) { http.onreadystatechange = function () {}; } } catch (e1) {}
        cb(ok, body || "");
      }
      try { http = new ActiveXObject("MSXML2.ServerXMLHTTP.6.0"); }
      catch (e2) { fin(false, ""); return; }
      /* 구버전엔 setProxy 가 없을 수 있어 감싼다. open 전후로 한 번씩 건다. */
      try { http.setProxy(1); } catch (ep1) {}
      try {
        http.setTimeouts(2000, 2000, 5000, 5000);
        http.open("GET", statusUrl(), true);
        try { http.setProxy(1); } catch (ep2) {}
        http.onreadystatechange = function () {
          var st = 0, tx = "";
          try { if (http.readyState != 4) { return; } } catch (e3) { fin(false, ""); return; }
          try { st = http.status; tx = http.responseText; } catch (e4) { st = 0; }
          fin(st == 200, tx);
        };
        http.send();
        wd = setTimeout(function () {
          try { http.abort(); } catch (e5) {}
          fin(false, "");
        }, 6000);
      } catch (e6) { fin(false, ""); }
    },

    /* 이 HTA 가 놓인 곳에서 프로그램 폴더를 찾아낸다 */
    resolveRoot: function () {
      var cand = [], i;
      try {
        var args = splitCmdLine(String(oHTA.commandLine));
        for (i = 1; i < args.length; i++) { cand[cand.length] = args[i]; }
      } catch (e) {}
      try {
        var p = location.pathname;
        try { p = decodeURIComponent(p); } catch (e1) {}
        p = p.replace(/\//g, "\\");
        if (/^\\[A-Za-z]:/.test(p)) { p = p.substring(1); }
        cand[cand.length] = p;
      } catch (e2) {}

      for (i = 0; i < cand.length; i++) {
        var c = trim(cand[i]);
        if (!c) { continue; }
        /* .hta 경로면 두 단계 위로 올라간다 */
        try {
          if (/\.hta$/i.test(c)) {
            c = fso.GetParentFolderName(fso.GetParentFolderName(c));
          }
          if (c && fso.FolderExists(c)) { return c; }
        } catch (e3) {}
      }
      return "";
    },

    quit: function () { try { window.close(); } catch (e) {} }
  };
}

/* mshta 명령줄을 따옴표 단위로 쪼갠다 */
function splitCmdLine(cl) {
  var out = [], i = 0, n = cl.length, cur, ch;
  while (i < n) {
    while (i < n && cl.charAt(i) === " ") { i++; }
    if (i >= n) { break; }
    cur = "";
    if (cl.charAt(i) === "\"") {
      i++;
      while (i < n) {
        ch = cl.charAt(i);
        if (ch === "\"") { i++; break; }
        cur += ch;
        i++;
      }
    } else {
      while (i < n && cl.charAt(i) !== " ") { cur += cl.charAt(i); i++; }
    }
    out[out.length] = cur;
  }
  return out;
}

var SYS = (typeof window.__MOCK_SYS !== "undefined" && window.__MOCK_SYS)
  ? window.__MOCK_SYS
  : makeRealSys();

/* ============================================================
   2. 화면
   ============================================================ */

var TITLES = ["프로그램 업데이트", "시세 데이터", "서버 시작", "화면 열기"];

function setSub(i, text) {
  var box = el("s" + i);
  if (!box) { return; }
  var subs = box.getElementsByTagName("div");
  var sub = null, k;
  for (k = 0; k < subs.length; k++) {
    if (subs[k].className.indexOf("sts") === 0) { sub = subs[k]; }
  }
  if (!sub) { return; }
  if (sub.innerHTML === text) { return; }
  sub.innerHTML = text;
  sub.className = "sts";
  /* 리플로 강제 -> 애니메이션 재시작 */
  var dummy = sub.offsetWidth;
  if (dummy < 0) { return; }
  sub.className = "sts fx";
}

function setState(i, state) {
  var box = el("s" + i);
  if (!box) { return; }
  box.className = "step is-" + state;
}

function setStep(i, state, sub) {
  setState(i, state);
  if (typeof sub === "string") { setSub(i, sub); }
}

function setHead(text) {
  var h = el("hsub");
  if (!h || h.innerHTML === text) { return; }
  h.innerHTML = text;
  h.className = "";
  var dummy = h.offsetWidth;
  if (dummy < 0) { return; }
  h.className = "fx";
}

function setNote(text) {
  var f = el("fnote");
  if (f) { f.innerHTML = text; }
}

function setProg(pct) {
  if (pct < 0) { pct = 0; }
  if (pct > 100) { pct = 100; }
  var b = el("bar");
  if (b) { b.style.width = pct + "%"; }
}

function setLogs(lines) {
  var i, t;
  for (i = 0; i < 3; i++) {
    t = lines[i] ? lines[i] : "&nbsp;";
    if (S.logShown[i] !== t) {
      S.logShown[i] = t;
      var g = el("lg" + i);
      if (g) { g.innerHTML = t; }
    }
  }
}

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function clipLine(s, n) {
  s = String(s);
  if (s.length <= n) { return s; }
  return s.substring(0, n - 1) + "…";
}

/* 로그 파일의 마지막 세 줄을 흘려 보낸다 */
function tailLog(path) { tailLogText(SYS.read(path)); }

function tailLogText(t) {
  if (!t) { return; }
  var lines = String(t).replace(/\r/g, "").split("\n");
  var keep = [], i, L;
  for (i = lines.length - 1; i >= 0 && keep.length < 3; i--) {
    L = trim(lines[i]);
    if (L) { keep.unshift(esc(clipLine(L, 76))); }
  }
  while (keep.length < 3) { keep.unshift(""); }
  setLogs(keep);
}

function showOv(id) {
  var o = el(id);
  if (o) { o.className = "ov on"; }
}

function hideOv(id) {
  var o = el(id);
  if (o) { o.className = "ov"; }
}

function drawCandles() {
  var box = el("candles");
  if (!box) { return; }
  var n = 22, html = "", i, h, w, top, up, x;
  var base = 26;
  for (i = 0; i < n; i++) {
    up = (i % 3 !== 1);
    /* 왼쪽에서 오른쪽으로 완만하게 올라가는 모양 */
    h = base + Math.round(i * 4.4) + ((i * 37) % 23);
    w = 6 + ((i * 13) % 14);
    top = h + 4 + ((i * 29) % 16);
    x = 6 + i * 23;
    html += '<div class="cd ' + (up ? "up" : "dn") + '" style="left:' + x + 'px;' +
            'animation-delay:' + (i * 18) + 'ms">' +
            '<i style="height:' + top + 'px"></i>' +
            '<b style="height:' + h + 'px"></b></div>';
  }
  box.innerHTML = html;
}

/* ============================================================
   3. 경로
   ============================================================ */

function buildPaths(root) {
  P.root = root;
  P.logs = root + "\\logs";
  P.work = root + "\\logs\\_launcher";
  P.venvPy = root + "\\.venv\\Scripts\\python.exe";
  P.pidFile = root + "\\.server.pid";
  P.serverLog = root + "\\logs\\server.log";
  P.statusJson = root + "\\data_status.json";
  P.marcap = root + "\\marcap";
  P.updMarcap = root + "\\update_marcap.bat";
  P.req = root + "\\requirements.txt";
  P.s1bat = P.work + "\\s1.bat";
  P.s1log = P.work + "\\s1.log";
  P.s2bat = P.work + "\\s2.bat";
  P.s2log = P.work + "\\s2.log";
  P.s3bat = P.work + "\\s3.bat";
  P.pbBat = P.work + "\\probe.bat";
  P.pbLog = P.work + "\\probe.log";
  P.cfgFile = root + "\\launcher_config.json";
}

/* ---------- 설정 파일 (없으면 기본값) ---------- */
function defaultCfg() { return { port: DEF_PORT, prog: true, quote: true }; }

function loadConfig() {
  var c = defaultCfg();
  var t = SYS.read(P.cfgFile);
  if (t) {
    var m = /"port"\s*:\s*(\d+)/.exec(String(t));
    if (m) {
      var v = parseInt(m[1], 10);
      if (v >= 1024 && v <= 65535) { c.port = v; }
    }
    if (/"auto_update_program"\s*:\s*false/.test(String(t))) { c.prog = false; }
    if (/"auto_update_quote"\s*:\s*false/.test(String(t))) { c.quote = false; }
  }
  return c;
}

function saveConfig() {
  var t = "{\r\n" +
    "  \"port\": " + S.cfg.port + ",\r\n" +
    "  \"auto_update_program\": " + (S.cfg.prog ? "true" : "false") + ",\r\n" +
    "  \"auto_update_quote\": " + (S.cfg.quote ? "true" : "false") + "\r\n" +
    "}\r\n";
  SYS.write(P.cfgFile, t, true);
}

/* ============================================================
   4. 신선도 판정
   ============================================================ */

/* 지금 시점에서 "받을 수 있는 가장 최근 거래일" */
function lastTradeDay(now) {
  var d = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  var dow = now.getDay();
  if (dow === 6) { d.setDate(d.getDate() - 1); return { date: d, why: "weekend" }; }
  if (dow === 0) { d.setDate(d.getDate() - 2); return { date: d, why: "weekend" }; }
  if (now.getHours() * 60 + now.getMinutes() >= CLOSE_MIN) {
    return { date: d, why: "today" };
  }
  do { d.setDate(d.getDate() - 1); } while (d.getDay() === 0 || d.getDay() === 6);
  return { date: d, why: "preclose" };
}

function readLastSync() {
  var t = SYS.read(P.statusJson);
  if (!t) { return ""; }
  var m = /"last_sync"\s*:\s*"([^"]*)"/.exec(String(t));
  return m ? trim(m[1]) : "";
}

function pickTradeDate(body) {
  if (!body) { return ""; }
  var m = /"latest_trade_date"\s*:\s*"([^"]+)"/.exec(String(body));
  return m ? trim(m[1]) : "";
}

/* 시세를 받으러 나갈 필요가 있는지 판단한다.
   skip 이면 네트워크에 아예 나가지 않는다. */
function decideQuotes() {
  var now = nowDate();
  var ltd = lastTradeDay(now);
  var avail = new Date(ltd.date.getFullYear(), ltd.date.getMonth(), ltd.date.getDate(), 18, 30, 0);

  if (S.force) { return { skip: false, ltd: ltd }; }

  /* 서버가 알려 준 실제 데이터 최신 거래일이 있으면 그게 가장 정확하다 */
  if (S.apiLatestTradeDate) {
    var api = parseStamp(S.apiLatestTradeDate);
    if (api && api.getTime() >= ltd.date.getTime()) {
      return { skip: true, why: "api", ltd: ltd, have: S.apiLatestTradeDate };
    }
  }

  var ls = parseStamp(S.lastSync);
  if (ls && ls.getTime() >= avail.getTime()) {
    return { skip: true, why: ltd.why, ltd: ltd, have: fmtDate(ltd.date) };
  }
  return { skip: false, ltd: ltd };
}

function quoteSkipMsg(dec) {
  var d = fmtDate(dec.ltd.date);
  if (dec.why === "api") { return "이미 최신입니다 (" + esc(dec.have) + " 데이터)"; }
  if (dec.why === "weekend") { return "주말이라 새 데이터가 없습니다 (마지막 거래일 " + d + ")"; }
  if (dec.why === "preclose") { return "장 마감 전이라 오늘 데이터는 아직 없습니다"; }
  return "이미 최신입니다 (" + d + " 데이터)";
}

/* ============================================================
   5. 배치 실행 공통
   ============================================================ */

function runBat(batPath, lines, donePath, logPath, timeoutMs, onTick, onEnd) {
  SYS.mkdir(P.logs);
  SYS.mkdir(P.work);
  SYS.del(donePath);
  SYS.del(logPath);

  var text = lines.join("\r\n") + "\r\n";
  if (!SYS.write(batPath, text, true)) {
    onEnd("writefail");
    return;
  }
  S.childPid = SYS.spawn("cmd.exe /c call \"" + batPath + "\"", P.root);

  var t0 = (new Date()).getTime();
  function tick() {
    if (S.cancelled) { return; }
    var done = false;
    try { done = SYS.exists(donePath); } catch (e) { done = false; }
    if (onTick) { try { onTick(); } catch (e1) {} }
    if (done) {
      S.childPid = 0;
      later(function () { onEnd("ok"); }, 80);
      return;
    }
    if ((new Date()).getTime() - t0 > timeoutMs) {
      onEnd("timeout");
      return;
    }
    later(tick, 400);
  }
  later(tick, 250);
}

function readOne(fileName) {
  return trim(SYS.read(P.work + "\\" + fileName));
}

/* ============================================================
   6. 1단계 - 프로그램 업데이트
   ============================================================ */

function s1Bat() {
  var W = P.work;
  return [
    "@echo off",
    "setlocal enabledelayedexpansion",
    "set \"ROOT=" + P.root + "\"",
    "set \"OUT=" + W + "\"",
    "set \"LOG=" + P.s1log + "\"",
    "set \"PY=" + P.venvPy + "\"",
    "set \"REQ=" + P.req + "\"",
    "set \"FORCE=" + (S.force ? "1" : "0") + "\"",
    "set \"ST=skip\"",
    "set \"OLD=\"",
    "set \"NEW=\"",
    "set \"DIRTY=\"",
    "> \"!OUT!\\s1_old.txt\" echo.",
    "> \"!OUT!\\s1_new.txt\" echo.",
    "> \"!OUT!\\s1_commits.txt\" echo.",
    ">> \"!LOG!\" echo [launcher] program update check",
    "where git >nul 2>&1",
    "if !errorlevel! neq 0 (",
    "    set \"ST=nogit\"",
    "    goto :fin",
    ")",
    "if not exist \"!ROOT!\\.git\" (",
    "    set \"ST=norepo\"",
    "    goto :fin",
    ")",
    "for /f %%a in ('git -C \"!ROOT!\" rev-parse HEAD 2^>nul') do set \"OLD=%%a\"",
    "> \"!OUT!\\s1_old.txt\" echo !OLD!",
    "for /f \"delims=\" %%a in ('git -C \"!ROOT!\" status --porcelain 2^>nul') do set \"DIRTY=1\"",
    "if defined DIRTY (",
    "    set \"ST=dirty\"",
    "    goto :fin",
    ")",
    "if \"!FORCE!\"==\"1\" goto :dopull",
    "rem ---- 원격 참조만 읽어 비교한다. 전체 fetch 를 하지 않는다 ----",
    "set \"BR=\"",
    "for /f \"tokens=1\" %%a in ('git -C \"!ROOT!\" rev-parse --abbrev-ref HEAD 2^>nul') do set \"BR=%%a\"",
    "if not defined BR (",
    "    set \"ST=detached\"",
    "    goto :fin",
    ")",
    "if \"!BR!\"==\"HEAD\" (",
    "    set \"ST=detached\"",
    "    goto :fin",
    ")",
    "set \"REMOTE=\"",
    ">> \"!LOG!\" echo [git] ls-remote origin !BR!",
    "for /f \"tokens=1\" %%a in ('git -C \"!ROOT!\" ls-remote origin \"refs/heads/!BR!\" 2^>nul') do set \"REMOTE=%%a\"",
    "if not defined REMOTE (",
    "    set \"ST=noremote\"",
    "    goto :fin",
    ")",
    "if /i \"!REMOTE!\"==\"!OLD!\" (",
    "    set \"ST=fresh\"",
    "    goto :fin",
    ")",
    ":dopull",
    ">> \"!LOG!\" echo [git] pull --ff-only",
    "git -C \"!ROOT!\" pull --ff-only >> \"!LOG!\" 2>&1",
    "if !errorlevel! neq 0 (",
    "    set \"ST=pullfail\"",
    "    goto :fin",
    ")",
    "for /f %%a in ('git -C \"!ROOT!\" rev-parse HEAD 2^>nul') do set \"NEW=%%a\"",
    "> \"!OUT!\\s1_new.txt\" echo !NEW!",
    "if /i \"!OLD!\"==\"!NEW!\" (",
    "    set \"ST=same\"",
    "    goto :fin",
    ")",
    "git -C \"!ROOT!\" log --oneline !OLD!..!NEW! > \"!OUT!\\s1_commits.txt\" 2>nul",
    "set \"ST=updated\"",
    "rem ---- requirements.txt 가 실제로 바뀐 경우에만 pip ----",
    "set \"REQCH=\"",
    "for /f \"delims=\" %%a in ('git -C \"!ROOT!\" diff --name-only !OLD! !NEW! 2^>nul') do (",
    "    if /i \"%%a\"==\"requirements.txt\" set \"REQCH=1\"",
    ")",
    "if not defined REQCH goto :fin",
    "if not exist \"!PY!\" goto :fin",
    ">> \"!LOG!\" echo [pip] requirements.txt changed - syncing packages",
    "\"!PY!\" -m pip install -q -r \"!REQ!\" >> \"!LOG!\" 2>&1",
    "set \"ST=updated_pip\"",
    ":fin",
    "> \"!OUT!\\s1_st.txt\" echo !ST!",
    "> \"!OUT!\\s1.done\" echo done",
    "endlocal"
  ];
}

function step1() {
  setStep(0, "run", "최신 버전이 있는지 확인합니다");
  setHead("프로그램을 확인하고 있습니다");
  setProg(6);

  if (!S.force && S.cfg && !S.cfg.prog) {
    finishStep(0, "skip", "설정에서 자동 업데이트를 꺼 두었습니다", step2);
    return;
  }
  if (!SYS.dirExists(P.root + "\\.git")) {
    finishStep(0, "skip", "git 으로 받은 폴더가 아니어서 건너뜁니다", step2);
    return;
  }

  runBat(P.s1bat, s1Bat(), P.work + "\\s1.done", P.s1log, 240000,
    function () { tailLog(P.s1log); },
    function (how) {
      if (S.cancelled) { return; }
      if (how === "timeout") {
        finishStep(0, "fail", "업데이트 확인이 너무 오래 걸립니다", step2);
        return;
      }
      if (how === "writefail") {
        finishStep(0, "skip", "임시 파일을 만들 수 없어 건너뜁니다", step2);
        return;
      }
      var st = readOne("s1_st.txt");
      var oldRev = readOne("s1_old.txt");
      var newRev = readOne("s1_new.txt");
      var commits = SYS.read(P.work + "\\s1_commits.txt");
      var lines = [], i, L;
      if (commits) {
        var raw = String(commits).replace(/\r/g, "").split("\n");
        for (i = 0; i < raw.length; i++) {
          L = trim(raw[i]);
          if (L) { lines[lines.length] = L; }
        }
      }
      var shortOld = oldRev ? oldRev.substring(0, 7) : "";
      var shortNew = newRev ? newRev.substring(0, 7) : "";

      if (st === "nogit") {
        finishStep(0, "skip", "git 이 없어 건너뜁니다", step2);
      } else if (st === "norepo") {
        finishStep(0, "skip", "git 으로 받은 폴더가 아니어서 건너뜁니다", step2);
      } else if (st === "dirty") {
        finishStep(0, "skip", "이 컴퓨터에서 고친 파일이 있어 업데이트를 건너뛰었습니다", step2);
      } else if (st === "detached") {
        finishStep(0, "skip", "특정 버전에 고정돼 있어 건너뜁니다", step2);
      } else if (st === "noremote") {
        finishStep(0, "skip", "받아올 곳을 찾지 못해 건너뜁니다", step2);
      } else if (st === "fresh") {
        finishStep(0, "skip", "이미 최신입니다 (" + shortOld + ") — 받지 않았습니다", step2);
      } else if (st === "same") {
        finishStep(0, "skip", "이미 최신입니다 (" + shortOld + ")", step2);
      } else if (st === "pullfail") {
        finishStep(0, "fail", "받기에 실패했습니다. 인터넷 연결을 확인하세요", step2);
      } else if (st === "updated" || st === "updated_pip") {
        S.tally.prog = 1;
        S.tally.progCommits = lines.length;
        S.tally.fetched = true;
        var msg = shortOld + " → " + shortNew;
        if (lines.length) { msg += " (" + lines.length + "건)"; }
        if (st === "updated_pip") { msg += " · 필요한 패키지도 맞췄습니다"; }
        if (lines.length) {
          var tail = [];
          for (i = lines.length - 1; i >= 0 && tail.length < 3; i--) {
            tail.unshift(esc(clipLine(lines[i], 76)));
          }
          while (tail.length < 3) { tail.unshift(""); }
          setLogs(tail);
        }
        finishStep(0, "done", msg, step2);
      } else {
        finishStep(0, "skip", "확인만 하고 넘어갑니다", step2);
      }
    });
}

/* ============================================================
   7. 2단계 - 시세 데이터
   ============================================================ */

function s2Bat() {
  var W = P.work;
  return [
    "@echo off",
    "setlocal enabledelayedexpansion",
    "set \"ROOT=" + P.root + "\"",
    "set \"OUT=" + W + "\"",
    "set \"LOG=" + P.s2log + "\"",
    "set \"MR=" + P.marcap + "\"",
    "set \"FORCE=" + (S.force ? "1" : "0") + "\"",
    "set \"ST=skip\"",
    "set \"CNT=0\"",
    "> \"!OUT!\\s2_cnt.txt\" echo 0",
    ">> \"!LOG!\" echo [launcher] marcap check",
    "where git >nul 2>&1",
    "if !errorlevel! neq 0 (",
    "    set \"ST=nogit\"",
    "    goto :fin",
    ")",
    "if not exist \"!MR!\\.git\" goto :dosync",
    "if \"!FORCE!\"==\"1\" goto :dosync",
    "set \"MOLD=\"",
    "for /f %%a in ('git -C \"!MR!\" rev-parse HEAD 2^>nul') do set \"MOLD=%%a\"",
    "set \"MREM=\"",
    ">> \"!LOG!\" echo [git] ls-remote marcap",
    "for /f \"tokens=1\" %%a in ('git -C \"!MR!\" ls-remote origin HEAD 2^>nul') do set \"MREM=%%a\"",
    "if not defined MREM goto :dosync",
    "if /i \"!MREM!\"==\"!MOLD!\" (",
    "    set \"ST=fresh\"",
    "    goto :fin",
    ")",
    ":dosync",
    "set \"MOLD2=\"",
    "for /f %%a in ('git -C \"!MR!\" rev-parse HEAD 2^>nul') do set \"MOLD2=%%a\"",
    ">> \"!LOG!\" echo [launcher] update_marcap.bat /silent",
    "call \"" + P.updMarcap + "\" /silent >> \"!LOG!\" 2>&1",
    "set \"MNEW=\"",
    "for /f %%a in ('git -C \"!MR!\" rev-parse HEAD 2^>nul') do set \"MNEW=%%a\"",
    "if not defined MNEW (",
    "    set \"ST=syncfail\"",
    "    goto :fin",
    ")",
    "if not defined MOLD2 (",
    "    set \"ST=cloned\"",
    "    goto :fin",
    ")",
    "if /i \"!MOLD2!\"==\"!MNEW!\" (",
    "    set \"ST=same\"",
    "    goto :fin",
    ")",
    "for /f %%a in ('git -C \"!MR!\" rev-list --count !MOLD2!..!MNEW! 2^>nul') do set \"CNT=%%a\"",
    "> \"!OUT!\\s2_cnt.txt\" echo !CNT!",
    "set \"ST=updated\"",
    ":fin",
    "> \"!OUT!\\s2_st.txt\" echo !ST!",
    "> \"!OUT!\\s2.done\" echo done",
    "endlocal"
  ];
}

function step2() {
  setStep(1, "run", "받을 시세가 있는지 확인합니다");
  setHead("시세 데이터를 확인하고 있습니다");
  setProg(30);

  S.lastSync = readLastSync();

  if (!S.force && S.cfg && !S.cfg.quote) {
    finishStep(1, "skip", "설정에서 자동 갱신을 꺼 두었습니다", step3);
    return;
  }
  if (!SYS.exists(P.updMarcap)) {
    finishStep(1, "skip", "update_marcap.bat 이 없어 건너뜁니다", step3);
    return;
  }

  /* 데이터 폴더가 통째로 없으면 1.8GB 를 마음대로 받지 않는다 */
  if (!SYS.dirExists(P.marcap)) {
    setSub(1, "시세 데이터가 아직 없습니다");
    setNote("확인을 기다리고 있습니다");
    showOv("ovAsk");
    return;
  }

  var dec = decideQuotes();
  if (dec.skip) {
    finishStep(1, "skip", quoteSkipMsg(dec), step3);
    return;
  }

  runQuoteSync(false);
}

function runQuoteSync(firstTime) {
  setStep(1, "run", firstTime
    ? "처음 받는 중입니다. 10~30분 걸립니다"
    : "새 시세를 받고 있습니다");
  runBat(P.s2bat, s2Bat(), P.work + "\\s2.done", P.s2log,
    firstTime ? 3600000 : 600000,
    function () { tailLog(P.s2log); },
    function (how) {
      if (S.cancelled) { return; }
      if (how === "timeout") {
        finishStep(1, "fail", "시세 받기가 너무 오래 걸립니다", step3);
        return;
      }
      if (how === "writefail") {
        finishStep(1, "skip", "임시 파일을 만들 수 없어 건너뜁니다", step3);
        return;
      }
      var st = readOne("s2_st.txt");
      var cnt = parseInt(readOne("s2_cnt.txt"), 10);
      if (isNaN(cnt)) { cnt = 0; }

      if (st === "nogit") {
        finishStep(1, "skip", "git 이 없어 건너뜁니다", step3);
      } else if (st === "fresh") {
        finishStep(1, "skip", "받을 새 데이터가 없습니다", step3);
      } else if (st === "same") {
        finishStep(1, "skip", "이미 최신입니다", step3);
      } else if (st === "cloned") {
        S.tally.quoteCloned = true;
        S.tally.fetched = true;
        finishStep(1, "done", "시세 데이터를 처음 받았습니다", step3);
      } else if (st === "updated") {
        S.tally.quoteDays = cnt;
        S.tally.fetched = true;
        finishStep(1, "done", cnt > 0 ? ("새 시세 " + cnt + "일치를 받았습니다") : "시세를 갱신했습니다", step3);
      } else if (st === "syncfail") {
        finishStep(1, "fail", "시세 받기에 실패했습니다. logs 폴더를 확인하세요", step3);
      } else {
        finishStep(1, "skip", "확인만 하고 넘어갑니다", step3);
      }
    });
}

/* ============================================================
   8. 포트 조사
   "HTTP 응답이 없다 = 서버가 없다" 가 아니다. 프록시 때문에 프로브가 막힐 수도,
   서버가 백테스트로 바쁠 수도 있다. 포트를 누가 잡고 있는지 반드시 따로 본다.
   ============================================================ */

function probeBat() {
  var W = P.work;
  return [
    "@echo off",
    "setlocal enabledelayedexpansion",
    "set \"OUT=" + W + "\"",
    "set \"PORT=" + S.port + "\"",
    "set \"PID=\"",
    "set \"PNAME=\"",
    "set \"PPATH=\"",
    "set \"FREE=\"",
    "set \"CAND=" + S.port + "\"",
    "rem ---- 설정된 포트를 누가 잡고 있는지 ----",
    "for /f \"tokens=2,5\" %%a in ('netstat -ano -p TCP 2^>nul ^| findstr \":!PORT!\"') do (",
    "    if \"%%a\"==\"127.0.0.1:!PORT!\" set \"PID=%%b\"",
    "    if \"%%a\"==\"0.0.0.0:!PORT!\" set \"PID=%%b\"",
    "    if \"%%a\"==\"[::1]:!PORT!\" set \"PID=%%b\"",
    "    if \"%%a\"==\"[::]:!PORT!\" set \"PID=%%b\"",
    ")",
    "if not defined PID goto :fin",
    "for /f \"tokens=1 delims=,\" %%c in ('tasklist /FI \"PID eq !PID!\" /FO CSV /NH 2^>nul') do set \"PNAME=%%~c\"",
    "rem ---- 실행 파일 경로까지 봐야 우리 .venv 파이썬인지 알 수 있다 ----",
    "for /f \"delims=\" %%d in ('powershell -NoProfile -ExecutionPolicy Bypass -Command \"try { (Get-Process -Id !PID! -ErrorAction Stop).Path } catch { '' }\" 2^>nul') do set \"PPATH=%%d\"",
    "rem ---- 비어 있는 포트도 하나 찾아 둔다 ----",
    "for /l %%i in (1,1," + PORT_TRIES + ") do (",
    "    if not defined FREE (",
    "        set /a \"CAND=CAND+1\"",
    "        call :chk",
    "    )",
    ")",
    ":fin",
    "> \"!OUT!\\pb_pid.txt\" echo.!PID!",
    "> \"!OUT!\\pb_name.txt\" echo.!PNAME!",
    "> \"!OUT!\\pb_path.txt\" echo.!PPATH!",
    "> \"!OUT!\\pb_free.txt\" echo.!FREE!",
    "> \"!OUT!\\pb.done\" echo done",
    "endlocal",
    "goto :eof",
    "",
    ":chk",
    "set \"BUSY=\"",
    "for /f \"tokens=2\" %%a in ('netstat -ano -p TCP 2^>nul ^| findstr \":!CAND!\"') do (",
    "    if \"%%a\"==\"127.0.0.1:!CAND!\" set \"BUSY=1\"",
    "    if \"%%a\"==\"0.0.0.0:!CAND!\" set \"BUSY=1\"",
    "    if \"%%a\"==\"[::1]:!CAND!\" set \"BUSY=1\"",
    "    if \"%%a\"==\"[::]:!CAND!\" set \"BUSY=1\"",
    ")",
    "if not defined BUSY set \"FREE=!CAND!\"",
    "goto :eof"
  ];
}

function probePort(cb) {
  runBat(P.pbBat, probeBat(), P.work + "\\pb.done", P.pbLog, 40000, null, function (how) {
    var info = { busy: false, pid: 0, name: "", path: "", free: 0, mine: false };
    if (how === "ok") {
      var pid = parseInt(readOne("pb_pid.txt"), 10);
      if (!isNaN(pid) && pid > 0) { info.busy = true; info.pid = pid; }
      var nm = readOne("pb_name.txt");
      if (/\.exe$/i.test(nm)) { info.name = nm; }
      info.path = readOne("pb_path.txt");
      var fp = parseInt(readOne("pb_free.txt"), 10);
      if (!isNaN(fp) && fp > 0) { info.free = fp; }
      if (info.busy) {
        /* 우리 서버인지는 실행 파일 경로로 판단한다.
           .server.pid 에 적힌 것은 감싼 cmd.exe 라 포트를 잡은 python 과 PID 가 다르다. */
        if (info.path && String(info.path).toLowerCase() === String(P.venvPy).toLowerCase()) {
          info.mine = true;
        }
        var saved = parseInt(trim(SYS.read(P.pidFile)), 10);
        if (!isNaN(saved) && saved === info.pid) { info.mine = true; }
      }
    }
    S.portInfo = info;
    cb(info);
  });
}

/* 한 번 실패했다고 단정하지 않는다. 1초 뒤 한 번 더 두드린다. */
function pingRobust(cb) {
  SYS.ping(function (ok, body) {
    if (ok) { cb(true, body); return; }
    later(function () {
      SYS.ping(function (ok2, body2) { cb(ok2, body2); });
    }, 1000);
  });
}

/* 우리가 띄웠던 서버가 죽은 채 PID 만 남아 있으면 치운다.
   반드시 이 폴더의 .venv 파이썬인지 확인한 뒤에만 죽인다. */
function cleanupZombie() {
  var t = SYS.read(P.pidFile);
  var pid = parseInt(trim(t), 10);
  if (!isNaN(pid) && pid > 0) { SYS.kill(pid); }
  SYS.del(P.pidFile);
  SYS.killVenvPython(P.venvPy);
}

/* ============================================================
   9. 3단계 - 서버 시작
   ============================================================ */

function s3Bat() {
  return [
    "@echo off",
    "cd /d \"" + P.root + "\"",
    "\"" + P.venvPy + "\" -m uvicorn server.app:app --host 127.0.0.1 --port " + S.port +
      " >> \"" + P.serverLog + "\" 2>&1"
  ];
}

function step3() {
  setStep(2, "run", "서버가 켜져 있는지 확인합니다");
  setHead("백테스트 엔진을 켜고 있습니다");
  setProg(56);

  pingRobust(function (ok, body) {
    if (S.cancelled) { return; }
    if (ok) {
      S.apiLatestTradeDate = pickTradeDate(body) || S.apiLatestTradeDate;
      finishStep(2, "skip", "이미 켜져 있습니다 (" + S.port + "번 포트)", step4);
      return;
    }
    /* 응답이 없다고 서버가 없다고 단정하지 않는다 */
    setSub(2, S.port + "번 포트를 누가 쓰고 있는지 확인합니다");
    probePort(function (info) {
      if (S.cancelled) { return; }
      if (!info.busy) { launchOurServer(true); return; }
      if (info.mine) {
        finishStep(2, "skip",
          "이미 실행 중인 서버를 그대로 씁니다 (PID " + info.pid + ")", step4);
        return;
      }
      showPortConflict(info, "");
    });
  });
}

function launchOurServer(cleanFirst) {
  if (!SYS.exists(P.venvPy)) {
    setStep(2, "fail", "설치가 아직 안 됐습니다");
    halt("install.bat 을 먼저 실행해 주세요");
    return;
  }
  if (cleanFirst) { cleanupZombie(); }
  startServer();
}

function startServer() {
  setSub(2, S.port + "번 포트로 서버를 켜는 중입니다");
  SYS.mkdir(P.logs);
  SYS.mkdir(P.work);

  /* 로그는 이어붙기라 지난 실행의 오류가 남아 있다. 지금부터 늘어난 부분만 본다. */
  var pre = SYS.read(P.serverLog);
  S.logMark = pre ? String(pre).length : 0;

  if (!SYS.write(P.s3bat, s3Bat().join("\r\n") + "\r\n", true)) {
    setStep(2, "fail", "임시 파일을 만들 수 없습니다");
    halt("run_web.bat 을 직접 실행해 보세요");
    return;
  }
  S.serverPid = SYS.spawn("cmd.exe /c call \"" + P.s3bat + "\"", P.root);
  if (S.serverPid) {
    SYS.write(P.pidFile, S.serverPid + "\r\n", true);
  }

  var t0 = (new Date()).getTime();
  var LIMIT = 60000;

  function wait() {
    if (S.cancelled) { return; }
    var el2 = (new Date()).getTime() - t0;
    var full = SYS.read(P.serverLog);
    var fresh = full ? String(full).substring(S.logMark) : "";
    tailLogText(fresh);

    /* 10048 = 포트 중복 바인딩. 60초를 다 기다릴 필요 없이 바로 원인을 짚어 준다. */
    if (fresh.indexOf("10048") >= 0) {
      setStep(2, "fail", S.port + "번 포트가 이미 쓰이고 있습니다");
      setSub(2, "누가 쓰고 있는지 확인합니다");
      probePort(function (info) { showPortConflict(info, "10048"); });
      return;
    }

    setProg(56 + Math.round(18 * (el2 / LIMIT)));
    setSub(2, "서버가 켜지기를 기다립니다 (" + Math.round(el2 / 1000) + "초)");

    SYS.ping(function (ok, body) {
      if (S.cancelled) { return; }
      if (ok) {
        S.tally.server = true;
        S.apiLatestTradeDate = pickTradeDate(body) || S.apiLatestTradeDate;
        finishStep(2, "done", "서버를 켰습니다 (" + S.port + "번 포트)", step4);
        return;
      }
      if ((new Date()).getTime() - t0 > LIMIT) {
        /* 시간이 다 됐어도 포트부터 다시 본다 */
        probePort(function (info) {
          if (info.busy && info.mine) {
            finishStep(2, "skip", "서버가 떠 있는 것으로 보입니다 (PID " + info.pid + ")", step4);
          } else if (info.busy) {
            showPortConflict(info, "10048");
          } else {
            setStep(2, "fail", "서버가 켜지지 않았습니다");
            halt("logs\\server.log 에 이유가 적혀 있습니다");
          }
        });
        return;
      }
      later(wait, 700);
    });
  }
  later(wait, 600);
}

/* 포트가 겹쳤을 때 무엇을 할지 사용자가 고르게 한다 */
function showPortConflict(info, why) {
  clearTimers();
  S.portInfo = info;
  setState(2, "fail");
  setSub(2, why === "10048"
    ? (S.port + "번 포트가 이미 쓰여 서버를 켜지 못했습니다")
    : (S.port + "번 포트를 다른 프로그램이 쓰고 있습니다"));
  setHead("포트가 겹칩니다");
  setNote("어떻게 할지 골라 주세요");

  var who = info.name ? ("<b>" + esc(info.name) + "</b>") : "<b>알 수 없는 프로그램</b>";
  var body = S.port + "번 포트를 이미 " + who;
  if (info.pid) { body += " <b>(PID " + info.pid + ")</b>"; }
  body += " 가 쓰고 있습니다.";
  if (info.path) { body += "<br><span class=\"pth\">" + esc(clipLine(info.path, 56)) + "</span>"; }
  if (why === "10048") { body += "<br>그래서 서버가 켜지지 못했습니다 (오류 10048)."; }
  body += "<br>KRX 백테스터의 이전 서버일 수도 있습니다.";
  body += info.free
    ? ("<br>비어 있는 포트: <b>" + info.free + "</b>")
    : "<br>주변에 비어 있는 포트를 찾지 못했습니다.";

  var m = el("portMsg");
  if (m) { m.innerHTML = body; }
  var ob = el("portOther");
  if (ob) {
    ob.disabled = info.free ? false : true;
    ob.innerHTML = info.free ? (info.free + "번 포트로 켜기") : "다른 포트로 켜기";
  }
  showOv("ovPort");
}

/* ============================================================
   10. 4단계 - 브라우저
   ============================================================ */

function step4() {
  setStep(3, "run", "기본 브라우저를 엽니다");
  setHead("화면을 여는 중입니다");
  setProg(88);
  later(function () {
    SYS.open(baseUrl());
    S.openedBrowser = true;
    finishStep(3, "done", "브라우저를 열었습니다", showDone);
  }, 300);
}

/* ============================================================
   11. 마무리 / 중단
   ============================================================ */

function finishStep(i, state, sub, next) {
  setStep(i, state, sub);
  setProg(Math.round((i + 1) * 24.5));
  if (next) { later(next, STEP_MIN); }
}

function summaryText() {
  var parts = [];
  if (S.tally.prog) {
    parts[parts.length] = "프로그램 업데이트 " +
      (S.tally.progCommits > 0 ? (S.tally.progCommits + "건") : "1건");
  }
  if (S.tally.quoteCloned) {
    parts[parts.length] = "시세 데이터 전체";
  } else if (S.tally.quoteDays > 0) {
    parts[parts.length] = "시세 " + S.tally.quoteDays + "일치";
  }
  if (!parts.length) {
    return S.tally.server
      ? "모두 최신입니다 — 바로 시작합니다"
      : "모두 최신입니다 — 바로 시작합니다";
  }
  return parts.join(", ") + "를 받았습니다";
}

function showDone() {
  S.finished = true;
  setProg(100);
  setHead("준비가 끝났습니다");
  setNote("곧 창이 닫힙니다");
  var d = el("dsum");
  if (d) { d.innerHTML = summaryText(); }
  showOv("ovDone");
  setTimeout(function () { SYS.quit(); }, DONE_HOLD);
}

/* 더 진행할 수 없을 때: 남은 단계를 건너뜀으로 표시하고 창을 열어 둔다 */
function halt(note) {
  clearTimers();
  S.finished = true;
  var i;
  for (i = 0; i < 4; i++) {
    var box = el("s" + i);
    if (box && box.className.indexOf("is-wait") >= 0) {
      setStep(i, "skip", "진행하지 못했습니다");
    }
  }
  setHead("계속할 수 없습니다");
  setNote(note);
  var b = el("btnCancel");
  if (b) { b.innerHTML = "닫기"; b.className = "pri"; }
}

function cancelAll() {
  S.cancelled = true;
  clearTimers();
  /* 아직 브라우저를 열지 않았는데 서버를 우리가 켰다면 되돌린다.
     업데이트 중인 git 은 중간에 끊으면 오히려 위험해서 건드리지 않는다. */
  if (S.serverPid && !S.openedBrowser) {
    SYS.kill(S.serverPid);
    SYS.del(P.pidFile);
  }
  SYS.quit();
}

function stopServer() {
  var t = SYS.read(P.pidFile);
  var pid = parseInt(trim(t), 10);
  if (!isNaN(pid) && pid > 0) { SYS.kill(pid); }
  SYS.del(P.pidFile);
  SYS.killVenvPython(P.venvPy);
}

/* ============================================================
   12. 이미 켜져 있을 때의 선택 화면
   ============================================================ */

function showToggle(body) {
  clearTimers();
  S.finished = true;
  var i;
  for (i = 0; i < 4; i++) { setStep(i, "skip", "확인하지 않았습니다"); }
  setStep(2, "done", "이미 켜져 있습니다 (" + S.port + "번 포트)");
  setProg(100);
  setHead("이미 실행 중입니다");
  setNote("");
  var m = el("runMsg");
  var extra = "";
  var d = pickTradeDate(body);
  if (d) { extra = "<br>가지고 있는 시세: <b>" + esc(d) + "</b> 까지"; }
  if (m) {
    m.innerHTML = "KRX 백테스터가 이미 켜져 있습니다." + extra + "<br>무엇을 할까요?";
  }
  showOv("ovRun");
}

/* ============================================================
   13. 시작
   ============================================================ */

function resetSteps() {
  var i;
  for (i = 0; i < 4; i++) { setStep(i, "wait", "대기 중"); }
  setProg(0);
  setLogs(["", "", ""]);
  S.tally = { prog: 0, progCommits: 0, quoteDays: 0, quoteCloned: false, server: false, fetched: false };
}

function restartFlow(force, note) {
  clearTimers();
  S.cancelled = false;
  S.finished = false;
  S.force = !!force;
  hideOv("ovAsk");
  hideOv("ovRun");
  hideOv("ovDone");
  hideOv("ovPort");
  hideOv("ovCfg");
  var b = el("btnCancel");
  b.innerHTML = "취소";
  b.className = "";
  resetSteps();
  setNote(note || "");
  later(step1, 120);
}

function wire() {
  el("btnCancel").onclick = function () {
    if (S.finished) { SYS.quit(); } else { cancelAll(); }
    return false;
  };
  el("frecheck").onclick = function () {
    restartFlow(true, "신선도 판정을 무시하고 전부 다시 확인합니다");
    return false;
  };

  /* ---- 설정 ---- */
  el("fcfg").onclick = function () {
    el("cfgPort").value = String(S.cfg ? S.cfg.port : DEF_PORT);
    el("cfgProg").checked = !(S.cfg && !S.cfg.prog);
    el("cfgQuote").checked = !(S.cfg && !S.cfg.quote);
    el("cfgErr").innerHTML = "";
    showOv("ovCfg");
    return false;
  };
  el("cfgCancel").onclick = function () { hideOv("ovCfg"); return false; };
  el("cfgSave").onclick = function () {
    var v = parseInt(trim(el("cfgPort").value), 10);
    if (isNaN(v) || v < 1024 || v > 65535) {
      el("cfgErr").innerHTML = "포트는 1024 ~ 65535 사이 숫자로 적어 주세요";
      return false;
    }
    if (!S.cfg) { S.cfg = defaultCfg(); }
    S.cfg.port = v;
    S.cfg.prog = !!el("cfgProg").checked;
    S.cfg.quote = !!el("cfgQuote").checked;
    saveConfig();
    S.port = v;
    restartFlow(false, "설정을 저장했습니다");
    return false;
  };

  /* ---- 포트 겹침 ---- */
  el("portUse").onclick = function () {
    hideOv("ovPort");
    S.cancelled = false;
    setNote("이미 떠 있는 서버를 씁니다");
    finishStep(2, "skip", "이미 쓰고 있는 서버에 붙었습니다", step4);
    return false;
  };
  el("portKill").onclick = function () {
    hideOv("ovPort");
    S.cancelled = false;
    var info = S.portInfo || {};
    setStep(2, "run", "쓰고 있던 프로그램을 종료합니다");
    setNote("");
    if (info.pid) { SYS.kill(info.pid); }
    cleanupZombie();
    later(function () { launchOurServer(false); }, 1600);
    return false;
  };
  el("portOther").onclick = function () {
    var info = S.portInfo || {};
    if (!info.free) { return false; }
    hideOv("ovPort");
    S.cancelled = false;
    S.port = info.free;
    if (!S.cfg) { S.cfg = defaultCfg(); }
    S.cfg.port = info.free;
    saveConfig();
    setStep(2, "run", info.free + "번 포트로 켭니다 (설정에 저장했습니다)");
    setNote("");
    later(function () { launchOurServer(false); }, 250);
    return false;
  };
  el("askYes").onclick = function () {
    hideOv("ovAsk");
    setNote("처음 한 번만 오래 걸립니다");
    runQuoteSync(true);
    return false;
  };
  el("askNo").onclick = function () {
    hideOv("ovAsk");
    setNote("");
    finishStep(1, "skip", "나중에 받기로 했습니다", step3);
    return false;
  };
  el("runOpen").onclick = function () {
    SYS.open(baseUrl());
    SYS.quit();
    return false;
  };
  el("runStop").onclick = function () {
    var m = el("runMsg");
    if (m) { m.innerHTML = "프로그램을 종료했습니다."; }
    stopServer();
    setTimeout(function () { SYS.quit(); }, 900);
    return false;
  };
  el("runClose").onclick = function () { SYS.quit(); return false; };
}

function fatal(msg) {
  clearTimers();
  S.finished = true;
  setHead("문제가 생겼습니다");
  setNote(msg);
  var b = el("btnCancel");
  if (b) { b.innerHTML = "닫기"; b.className = "pri"; }
}

function boot() {
  drawCandles();
  wire();
  resetSteps();

  var root = "";
  try { root = SYS.resolveRoot(); } catch (e) { root = ""; }
  if (!root) {
    fatal("프로그램 폴더를 찾지 못했습니다. KRX백테스터.vbs 로 실행해 주세요.");
    return;
  }
  buildPaths(root);
  S.cfg = loadConfig();
  S.port = S.cfg.port;

  setHead("서버가 켜져 있는지 확인합니다");
  pingRobust(function (ok, body) {
    if (ok) { showToggle(body); return; }
    later(step1, 200);
  });
}

window.onerror = function (msg) {
  try { fatal("예상치 못한 오류: " + msg); } catch (e) {}
  return true;
};

/* HTA 에서만 창 크기를 잡는다 (브라우저 검증용 페이지에서는 건드리지 않는다) */
function sizeWindow() {
  try {
    if (typeof oHTA === "undefined") { return; }
    var w = 524, h = 430;
    window.resizeTo(w, h);
    var x = Math.max(0, Math.round((screen.availWidth - w) / 2));
    var y = Math.max(0, Math.round((screen.availHeight - h) / 2.4));
    window.moveTo(x, y);
  } catch (e) {}
}

sizeWindow();

if (window.__MOCK_MANUAL_BOOT) {
  window.__bootLauncher = boot;
} else {
  boot();
}

})();
