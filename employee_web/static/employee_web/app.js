/* Infome's mobile palette and components, backed by the shared employee APIs. */
"use strict";
(() => {
  const app = document.getElementById("app");
  const screen = document.getElementById("screen");
  const sheet = document.getElementById("sheet");
  const sheetBody = document.getElementById("sheet-body");
  const csrf = document.querySelector("#logout-form [name=csrfmiddlewaretoken]").value;
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const number = value => value == null ? "—" : new Intl.NumberFormat(undefined, {maximumFractionDigits: 1}).format(value);
  const date = value => value ? new Date(value.length === 10 ? `${value}T12:00:00` : value).toLocaleDateString(undefined, {day: "numeric", month: "short"}) : "—";
  const timestamp = value => value ? new Date(value).toLocaleString(undefined, {day: "numeric", month: "short", hour: "numeric", minute: "2-digit"}) : "Not synced yet";
  const initials = name => String(name).split(/\s+/).filter(Boolean).slice(0, 2).map(p => p[0]).join("").toUpperCase();
  const paths = {
    home: '<path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z"/>',
    trophy: '<path d="M8 3h8v6a4 4 0 0 1-8 0V3Zm4 10v5m-4 3h8M8 5H4v3a4 4 0 0 0 4 4m8-7h4v3a4 4 0 0 1-4 4M9 18h6"/>',
    rankings: '<path d="M4 21V11h5v10m1 0V3h5v18m1 0V7h5v14M2 21h21"/>',
    community: '<circle cx="9" cy="7" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3m1-17a3 3 0 0 1 0 6m2 3a5 5 0 0 1 3 5v3"/>',
    refresh: '<path d="M20 7V3l-3 3a8 8 0 1 0 2 11M20 3v5h-5"/>',
    fire: '<path d="M12 3c1 5 6 5 6 11a6 6 0 0 1-12 0c0-3 2-5 3-6 0 3 2 3 2 3s2-3 1-8Z"/>',
    pin: '<path d="M19 10c0 5-7 11-7 11S5 15 5 10a7 7 0 0 1 14 0Z"/><circle cx="12" cy="10" r="2"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    phone: '<rect x="6" y="2" width="12" height="20" rx="3"/><path d="M10 18h4M10 5h4"/>',
  };
  const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.trophy}</svg>`;
  document.querySelectorAll("[data-icon]").forEach(el => { el.innerHTML = icon(el.dataset.icon); });
  let me, route = "home", board = "steps_week", challengeTab = "open", challenges, mine, ranking;
  let posts = [], nextPage = null, controller, version = 0, loading = false, toastTimer, photoURL, dialogVersion = 0;

  async function api(path, {method = "GET", data, signal} = {}) {
    const abort = new AbortController();
    let timedOut = false;
    const cancel = () => abort.abort();
    if (signal?.aborted) abort.abort();
    else signal?.addEventListener("abort", cancel, {once: true});
    const timer = setTimeout(() => { timedOut = true; abort.abort(); }, 45000);
    try {
      const headers = {Accept: "application/json"};
      if (method !== "GET") headers["X-CSRFToken"] = csrf;
      const multipart = data instanceof FormData;
      if (data && !multipart) headers["Content-Type"] = "application/json";
      const response = await fetch(app.dataset.api + path, {method, headers, credentials: "same-origin",
        cache: "no-store", body: data ? (multipart ? data : JSON.stringify(data)) : undefined, signal: abort.signal});
      if (response.status === 401) {
        screen.innerHTML = "";
        sheet.close();
        location.replace(app.dataset.login);
        throw new Error("Your session has ended. Sign in with your current token.");
      }
      const payload = response.status === 204 ? null : await response.json().catch(() => null);
      if (!response.ok) {
        const error = payload?.error;
        const details = error?.details;
        const message = details && typeof details === "object"
          ? Object.entries(details).map(([key, value]) => `${key === "non_field_errors" ? "" : key + ": "}${Array.isArray(value) ? value.join(" ") : String(value)}`).join(" ")
          : error?.message;
        throw new Error(message || "Something went wrong. Please try again.");
      }
      return payload;
    } catch (error) {
      if (timedOut) throw new Error("The request took too long. Check your connection and try again.");
      if (error instanceof TypeError) throw new Error("Could not connect. Check your connection and try again.");
      throw error;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", cancel);
    }
  }

  function toast(message) {
    const el = document.getElementById("toast");
    clearTimeout(toastTimer);
    el.textContent = message;
    el.hidden = false;
    toastTimer = setTimeout(() => { el.hidden = true; }, 5500);
  }
  function empty(title, text, symbol = "trophy", action = "") {
    return `<div class="card empty"><span class="icon-tile">${icon(symbol)}</span><h3>${esc(title)}</h3><p>${esc(text)}</p>${action}</div>`;
  }
  function rankLink(data) {
    return `<a class="card card-link" href="#rankings"><span class="icon-tile">${icon("rankings")}</span><div class="grow"><strong>${data?.me ? `You are #${data.me.rank} of ${data.participants}` : "Live rankings"}</strong><span class="small muted">Steps this week · your company</span></div><span class="arrow small">Open →</span></a>`;
  }
  function syncNote() {
    return '<p class="notice">Steps arrive through <strong>HealthySync</strong>. Open it and run <strong>Sync last 24 hours</strong> for a catch-up, then refresh here. Background syncing follows iOS scheduling.</p>';
  }

  async function home(signal) {
    const [today, week, earned, ranks] = await Promise.all([
      api("home/", {signal}), api("steps/?period=week", {signal}),
      api("challenges/mine/", {signal}), api("rankings/?board=steps_week", {signal}),
    ]);
    const days = await api(`activity/?from=${week.from}&to=${week.to}`, {signal});
    const steps = today.metrics.steps, goal = today.step_goal;
    const percentage = steps == null ? 0 : Math.min(100, Math.max(0, Math.round(steps / Math.max(goal, 1) * 100)));
    const hour = new Date().getHours();
    const greeting = hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
    const stats = [["fire", today.metrics.active_kcal, "kcal", "Active energy"],
      ["pin", today.metrics.distance_m == null ? null : today.metrics.distance_m / 1000, "km", "Distance"],
      ["clock", today.metrics.exercise_minutes, "min", "Exercise"]];
    const bars = [];
    const byDate = new Map(days.map(day => [day.date, day.metrics.steps]));
    const max = Math.max(1, goal, ...days.map(day => day.metrics.steps || 0));
    for (let i = 0; i < 7; i++) {
      const day = new Date(`${week.from}T12:00:00Z`);
      day.setUTCDate(day.getUTCDate() + i);
      const key = day.toISOString().slice(0, 10), value = byDate.get(key);
      const label = `${date(key)}: ${value == null ? "No synced data" : number(value) + " steps"}`;
      bars.push(`<div title="${esc(label)}" aria-label="${esc(label)}"><div class="bar-track ${value == null ? "missing" : ""}">${value != null ? `<span style="height:${Math.max(2, value / max * 100)}%"></span>` : ""}</div><small>${esc(day.toLocaleDateString(undefined, {weekday: "narrow", timeZone: "UTC"}))}</small></div>`);
    }
    const active = earned.challenges.filter(c => !c.complete && c.end_date >= today.date).slice(0, 2);
    screen.innerHTML = `<div class="stack">
      <div class="screen-heading"><p class="eyebrow">${greeting}</p><h1>${me.first_name ? `Hi ${esc(me.first_name)},` : "Hey there,"}<br>let's move today</h1><p class="small muted">${esc(date(today.date))} · ${today.last_successful_sync_at ? `Synced ${esc(timestamp(today.last_successful_sync_at))}` : "Waiting for your first sync"}</p></div>
      <div class="hero"><div class="step-ring"><svg viewBox="0 0 120 120" aria-hidden="true"><circle cx="60" cy="60" r="52" fill="none" stroke="#ffffff38" stroke-width="11"/><circle cx="60" cy="60" r="52" fill="none" stroke="white" stroke-width="11" stroke-linecap="round" stroke-dasharray="${percentage * 3.267} 327"/></svg><div><strong>${steps == null ? number(goal) : percentage + "%"}</strong><small>${steps == null ? "daily goal" : "of goal"}</small></div></div><div class="grow"><p class="eyebrow">Today</p><div class="step-count">${number(steps)}</div><p class="small">steps of ${number(goal)}</p>${steps == null ? '<p class="small muted">No synced steps for today</p>' : `<span class="pill">${steps >= goal ? "Goal reached!" : number(goal - steps) + " to go"}</span>`}</div></div>
      <div class="stats">${stats.map(([symbol, value, unit, label]) => `<div class="card stat"><span class="icon-tile">${icon(symbol)}</span><strong>${number(value)} <small>${unit}</small></strong><small>${label}</small></div>`).join("")}</div>
      ${rankLink(ranks)}
      <div class="card"><div class="row between"><div><p class="eyebrow">Your week</p><h2>${number(week.steps)} <span class="small muted">steps</span></h2></div><span class="pill">${week.days_with_data} synced days</span></div><div class="week-bars">${bars.join("")}</div><p class="small muted" style="margin-top:12px">Missing days are not counted as zero.</p></div>
      <div class="section-title"><h2>Your challenges</h2><a class="text-button" href="#challenges">View all →</a></div>
      ${active.length ? active.map(challengeCard).join("") : empty("Every step can count", "Join a company challenge and work towards your next win.", "trophy", '<a class="button outline" href="#challenges">Explore challenges</a>')}
      <a href="#community" class="card card-link"><span class="icon-tile">${icon("community")}</span><div class="grow"><strong>Better together</strong><span class="small muted">Share a win. Cheer on your team.</span></div><span class="arrow">→</span></a>
      ${syncNote()}
    </div>`;
  }

  function challengeCard(c) {
    const value = c.kind === "steps" ? c.verified_steps : c.kind === "running" ? c.verified_running_minutes : c.progress_value;
    const fraction = c.target > 0 && value != null ? Math.min(100, Math.max(0, value / c.target * 100)) : 0;
    return `<article class="card challenge stack"><div class="row"><span class="icon-tile">${icon("trophy")}</span><div class="grow"><div class="row wrap"><p class="eyebrow">${esc(c.kind_label || c.kind)}</p>${c.complete ? '<span class="pill success">Completed</span>' : ""}</div><h3>${esc(c.title)}</h3><p class="small muted">Goal: ${esc(c.target_label)} · ${esc(date(c.start_date))}${c.start_date !== c.end_date ? " – " + esc(date(c.end_date)) : ""}</p></div></div>
      ${c.description ? `<p class="small muted post-body">${esc(c.description)}</p>` : ""}
      ${c.joined ? (c.upcoming ? `<span class="pill">Joined · counting starts ${esc(date(c.start_date))}</span>` : `<div class="stack" style="gap:7px"><div class="progress-label"><span>${esc(c.progress_label || "No synced data yet")}</span><strong>${Math.floor(fraction)}%</strong></div><progress value="${fraction}" max="100" aria-label="${esc(c.title)} progress"></progress></div>`) : `<div class="row between"><span class="small muted">${c.upcoming ? "Starts " + esc(date(c.start_date)) : "Running now"}<br>+${number(c.reward_points)} points</span>${c.can_join ? `<button class="button outline" data-action="join" data-id="${c.id}">+ Join</button>` : '<span class="pill">Ended</span>'}</div>`}
      ${c.joined ? `<p class="small muted">${c.complete ? "Reward" : "On completion"}: ${number(c.reward_points)} points${c.badge_title ? " · " + esc(c.badge_title) : ""}</p>` : ""}
      ${c.counting_note && !c.complete ? `<p class="small subtle">${esc(c.counting_note)}</p>` : ""}</article>`;
  }
  function renderChallenges() {
    const all = new Map(challenges.map(c => [c.id, c]));
    mine.challenges.forEach(c => all.set(c.id, c));
    const list = [...all.values()].filter(c => challengeTab === "mine" ? c.joined : !c.joined);
    if (challengeTab === "mine") list.sort((a, b) => Number(a.complete) - Number(b.complete));
    screen.innerHTML = `<div class="stack"><div class="row between screen-heading"><div><p class="eyebrow">Company</p><h1>Challenges</h1></div><span class="pill dark">${number(mine.points_total)} pts</span></div>
      <div class="segments" aria-label="Challenge filter"><button data-action="challenge-tab" data-value="open" aria-pressed="${challengeTab === "open"}">Open to join</button><button data-action="challenge-tab" data-value="mine" aria-pressed="${challengeTab === "mine"}">My challenges</button></div>
      ${rankLink(null)}
      ${list.length ? list.map(challengeCard).join("") : empty(challengeTab === "mine" ? "Your next win starts here" : "You're all caught up", challengeTab === "mine" ? "Pick a challenge from Open to join. Your synced activity will track your progress." : "No challenges open to join right now. Check back for the next one.")}
      ${challengeTab === "mine" ? `<div class="card stack"><h3>Your badges</h3>${mine.badges.length ? `<div class="row wrap">${mine.badges.map(b => `<span class="pill gold">${icon("trophy")} ${esc(b.title)}</span>`).join("")}</div>` : '<p class="small muted">Complete a challenge to earn your first badge.</p>'}</div>` : ""}
      <p class="notice">Progress counts automatically from synced phone or watch records. Manually entered activity does not earn challenge credit. Points arrive when you complete a challenge.</p></div>`;
  }

  const boards = {steps_today: "Today", steps_week: "This week", steps_month: "This month", points: "Points", teams: "Teams"};
  function rankRow(row) {
    return `<div class="rank-row ${row.is_me ? "me" : ""}"><span class="rank-number">${row.rank}</span><div class="grow"><strong>${esc(row.name)}</strong>${row.detail ? `<p class="small muted">${esc(row.detail)}</p>` : ""}</div><strong aria-label="${esc(row.value_label)}">${number(row.value)}</strong></div>`;
  }
  function renderRankings() {
    const data = ranking, own = data.me;
    const ahead = own && data.rows.find(r => r.rank === own.rank - 1);
    const gap = ahead && ahead.value - own.value;
    const podium = data.rows.length >= 3 ? [data.rows[1], data.rows[0], data.rows[2]] : [];
    const rest = podium.length ? data.rows.slice(3) : data.rows;
    if (own && !data.rows.some(r => r.is_me)) rest.push(own);
    screen.innerHTML = `<div class="stack"><div class="row between screen-heading"><div><p class="eyebrow">A little friendly competition</p><h1>Rankings</h1></div><span class="pill success"><span class="live-dot"></span>Live</span></div>
      <div class="chips" aria-label="Rankings period">${Object.entries(boards).map(([key, label]) => `<button class="chip" data-action="board" data-value="${key}" aria-pressed="${board === key}">${label}</button>`).join("")}</div>
      <div class="hero"><span class="rank-circle">${own ? "#" + own.rank : "—"}</span><div class="grow"><p class="eyebrow">${esc(data.title)}</p>${own ? `<h2>You are #${own.rank} of ${data.participants}</h2><p class="small">${esc(own.value_label)}${gap > 0 ? ` · ${number(gap)} behind #${ahead.rank}` : ""}</p>` : `<p>${esc(data.message ? data.message.replace("Open the app to sync.", "Open HealthySync to sync.") : "You are not on this board yet.")}</p>`}</div></div>
      ${podium.length ? `<div class="podium">${podium.map(row => `<div class="card podium-card ${row.rank === 1 ? "first" : row.rank === 2 ? "second" : "third"} ${row.is_me ? "me" : ""}"><span class="avatar">${esc(row.is_me ? "You" : initials(row.name))}</span><strong>${esc(row.name)}</strong><span class="small muted" aria-label="${esc(row.value_label)}">${number(row.value)}</span><div class="place">${row.rank}</div></div>`).join("")}</div>` : ""}
      ${rest.length ? `<div class="card rank-list">${rest.map(rankRow).join("")}</div>` : !data.rows.length ? empty("A fresh starting line", "Nobody is on this board yet. Synced activity and completed challenges bring it to life.", "rankings") : ""}
      ${board !== "teams" ? `<label class="card switch-row"><div class="grow"><strong>Show my name to colleagues</strong><p class="small muted">When off, you appear as “Colleague”.</p></div><input type="checkbox" id="show-name" ${data.show_my_name ? "checked" : ""}></label>` : ""}
      <p class="small muted">Rankings use eligible steps from each person's active phone or watch. HealthySync uploads update iPhone steps; background timing depends on iOS.</p><p class="small subtle">Updated ${esc(timestamp(data.updated_at))} · refreshes every 30 seconds while open</p></div>`;
  }

  function postCard(post) {
    const reactions = [["like", "♡", post.likes_count, post.user_liked, "Like"], ["celebrate", "🎉", post.celebrate_count, post.user_celebrated, "Celebrate"], ["fire", "🔥", post.fire_count, post.user_fired, "Fire"]];
    return `<article class="card post stack" data-post="${post.id}"><div class="row"><span class="avatar">${esc(initials(post.author_name))}</span><div class="grow"><strong>${esc(post.author_name)}</strong><p class="small muted">${esc(post.visibility === "team" ? "Team: " + post.team_name : "Company-wide")} · ${esc(timestamp(post.created_at))}</p>${post.author_role ? `<p class="small muted">${esc(post.author_role)}</p>` : ""}</div></div>
      ${post.status === "review" ? '<span class="pill gold">In review · only visible to you</span>' : ""}
      ${post.body ? `<p class="post-body">${esc(post.body)}</p>` : ""}
      ${post.has_image ? `<div><img class="post-photo" src="${app.dataset.api}community/posts/${post.id}/photo/" loading="lazy" alt="Photo shared by ${esc(post.author_name)}"><button class="text-button photo-retry" data-action="photo-retry" hidden>Photo unavailable · retry</button></div>` : ""}
      ${post.hashtags.length ? `<p class="post-tags">${post.hashtags.map(esc).join(" ")}</p>` : ""}
      <div class="post-actions">${reactions.map(([kind, symbol, count, active, label]) => `<button class="reaction" data-action="react" data-id="${post.id}" data-kind="${kind}" aria-label="${label}, ${count}" aria-pressed="${active}">${symbol} ${count}</button>`).join("")}<button class="reaction" data-action="comments" data-id="${post.id}" aria-label="Comments, ${post.comments_count}">💬 ${post.comments_count}</button></div>
      <div class="post-footer"><button class="text-button muted" data-action="report" data-id="${post.id}">Report</button>${post.can_delete ? `<button class="text-button" data-action="delete" data-id="${post.id}">Remove</button>` : ""}</div></article>`;
  }
  function renderCommunity() {
    screen.innerHTML = `<div class="stack"><div class="screen-heading"><p class="eyebrow">Better together</p><h1>Community</h1><p class="muted small">Small wins. Big encouragement. Your people.</p></div>
      <button class="card card-link wide" data-action="compose"><span class="avatar">${esc(initials(me.name))}</span><span class="grow muted" style="text-align:left">Share a little win…</span><span class="arrow small">Write a post</span></button>
      <p class="small muted">Posts from your team and colleagues sharing company-wide.</p>
      <div id="posts" class="stack">${posts.length ? posts.map(postCard).join("") : empty("Start a conversation", "Share a walk, a milestone, or a little motivation with your colleagues.", "community")}</div>
      ${nextPage ? '<button class="button wide" data-action="more-posts">Load more</button>' : ""}</div>`;
  }
  function rememberPage(data, append = false) {
    posts = append ? [...new Map([...posts, ...data.results].map(p => [p.id, p])).values()] : data.results;
    nextPage = data.next ? Number(new URL(data.next, location.href).searchParams.get("page")) : null;
  }
  function replacePost(updated) {
    posts = posts.map(p => p.id === updated.id ? updated : p);
    const el = screen.querySelector(`[data-post="${updated.id}"]`);
    if (el) el.outerHTML = postCard(updated);
  }

  function openSheet(title, html) {
    dialogVersion++;
    if (photoURL) { URL.revokeObjectURL(photoURL); photoURL = null; }
    document.getElementById("sheet-title").textContent = title;
    sheetBody.innerHTML = html;
    if (!sheet.open) sheet.showModal();
  }
  function formError(form, error) {
    const target = form.querySelector("[data-form-error]");
    target.textContent = error.message;
    target.hidden = false;
  }
  const errorSlot = '<p class="error" data-form-error role="alert" hidden></p>';
  function compose() {
    openSheet("Write a post", `<form id="post-form"><div class="form-field"><label for="post-body">Your update</label><textarea id="post-body" name="body" maxlength="2000" rows="4" placeholder="A morning walk, a new personal best, a little motivation…"></textarea></div>
      <div class="form-field"><label for="post-visibility">Share with</label><select id="post-visibility" name="visibility">${me.team ? `<option value="team">My team · ${esc(me.team)}</option>` : ""}<option value="company">Whole company</option></select></div>
      <div class="form-field"><label for="post-tags">Hashtags (optional)</label><input id="post-tags" name="tags" placeholder="#SmallWins #TeamWalk" maxlength="320"></div>
      <div class="form-field"><label for="post-photo">Photo (optional)</label><input id="post-photo" name="image" type="file" accept="image/jpeg,image/png,image/webp"><p class="small muted">JPEG, PNG or WebP · up to 5 MB</p><img id="photo-preview" class="preview-photo" alt="Selected photo preview" hidden></div>
      <p class="small muted">Posts get a quick automatic check. Some may wait for staff review.</p>${errorSlot}<button class="button primary wide" type="submit" style="margin-top:15px">Publish post</button></form>`);
  }
  async function comments(id) {
    openSheet("Conversation", `<div id="comments-list" class="stack"><p class="muted small">Loading comments…</p></div><form id="comment-form" data-id="${id}" style="margin-top:18px"><label for="comment-body">Add a comment</label><textarea id="comment-body" name="body" required maxlength="500" rows="2"></textarea>${errorSlot}<button class="button primary wide" type="submit" style="margin-top:12px">Send comment</button></form>`);
    const current = dialogVersion;
    try {
      const list = await api(`community/posts/${id}/comments/`);
      if (current === dialogVersion && sheet.open) drawComments(list);
    } catch (error) {
      if (current === dialogVersion && sheet.open) document.getElementById("comments-list").innerHTML = `<p class="error">${esc(error.message)}</p><button class="button" data-action="comments" data-id="${id}">Retry comments</button>`;
    }
  }
  function drawComments(list) {
    document.getElementById("comments-list").innerHTML = list.length ? list.map(c => `<div class="comment"><div class="row between"><strong>${esc(c.author_name)}</strong><span class="small muted">${esc(timestamp(c.created_at))}</span></div><p class="comment-body">${esc(c.body)}</p></div>`).join("") : '<p class="small muted">No comments yet. Be the first to cheer them on.</p>';
  }
  function profile() {
    openSheet("Your profile", `<div class="stack profile-info"><div class="row"><span class="avatar dark">${esc(initials(me.name))}</span><div class="grow"><h3>${esc(me.name)}</h3><p class="small muted">${esc(me.email)}</p></div></div><div class="card"><p class="eyebrow">Your team</p><strong>${esc(me.team || "Not assigned yet")}</strong><p class="small muted">Ask HR to update your employee details.</p></div><div class="card"><p class="eyebrow">iPhone connection</p><strong>${esc(me.device_label || "HealthySync")}</strong><p class="small muted">Last sync: ${esc(timestamp(me.last_sync))}</p></div>${syncNote()}<p class="small muted">For quick access: Safari → Share → Add to Home Screen.</p><button class="button wide" data-action="logout">Sign out</button></div>`);
  }

  async function load({focus = false} = {}) {
    const current = ++version;
    controller?.abort();
    controller = new AbortController();
    const signal = controller.signal;
    route = ["home", "challenges", "rankings", "community"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "home";
    document.querySelectorAll("[data-tab]").forEach(el => {
      if (el.dataset.tab === route) el.setAttribute("aria-current", "page");
      else el.removeAttribute("aria-current");
    });
    document.title = `${route[0].toUpperCase() + route.slice(1)} · Infome Fit`;
    loading = true;
    screen.setAttribute("aria-busy", "true");
    screen.innerHTML = '<div class="loading" role="status">Loading…</div>';
    try {
      if (!me) me = await api("me/", {signal});
      document.getElementById("profile").textContent = initials(me.name) || "Me";
      if (route === "home") await home(signal);
      else if (route === "challenges") {
        [challenges, mine] = await Promise.all([api("challenges/", {signal}), api("challenges/mine/", {signal})]);
        if (current === version) renderChallenges();
      } else if (route === "rankings") {
        ranking = await api(`rankings/?board=${board}`, {signal});
        if (current === version) renderRankings();
      } else {
        const data = await api("community/posts/", {signal});
        if (current === version) { rememberPage(data); renderCommunity(); }
      }
    } catch (error) {
      if (current === version && error.name !== "AbortError") screen.innerHTML = empty("Let's try that again", error.message, "refresh", '<button class="button outline" data-action="retry">Try again</button>');
    } finally {
      if (current === version) {
        loading = false;
        screen.setAttribute("aria-busy", "false");
        if (focus) screen.focus({preventScroll: true});
      }
    }
  }

  async function action(button) {
    const name = button.dataset.action, id = Number(button.dataset.id), current = version;
    if (name === "compose") return compose();
    if (name === "comments") return comments(id);
    if (name === "logout") return document.getElementById("logout-form").requestSubmit();
    if (name === "retry") return load();
    if (name === "board") { board = button.dataset.value; return load(); }
    if (name === "challenge-tab") { challengeTab = button.dataset.value; return renderChallenges(); }
    if (name === "delete") return openSheet("Remove this post?", `<p class="muted">It will no longer appear in the community feed.</p><form id="delete-form" data-id="${id}">${errorSlot}<button type="submit" class="button primary wide" style="margin-top:20px">Remove post</button></form>`);
    if (name === "report") return openSheet("Report a post", `<form id="report-form" data-id="${id}"><label for="report-reason">What should the staff know?</label><textarea id="report-reason" name="reason" maxlength="500" required></textarea>${errorSlot}<button class="button primary wide" type="submit" style="margin-top:15px">Send report</button></form>`);
    button.disabled = true;
    try {
      if (name === "join") {
        const joined = await api(`challenges/${id}/join/`, {method: "POST", data: {}});
        toast(`Joined ${joined.title}. Let's do this!`);
        if (current === version) { challengeTab = "mine"; await load(); }
      } else if (name === "react") {
        const post = posts.find(p => p.id === id);
        const kind = button.dataset.kind;
        const key = {like: "user_liked", celebrate: "user_celebrated", fire: "user_fired"}[kind];
        const updated = await api(`community/posts/${id}/reactions/${kind}/`, {method: "PUT", data: {active: !post[key]}});
        if (current === version) replacePost(updated);
      } else if (name === "more-posts") {
        const data = await api(`community/posts/?page=${nextPage}`);
        if (current === version) {
          const oldIds = new Set(posts.map(p => p.id));
          rememberPage(data, true);
          document.getElementById("posts").insertAdjacentHTML("beforeend", posts.filter(p => !oldIds.has(p.id)).map(postCard).join(""));
          if (!nextPage) button.remove();
        }
      } else if (name === "photo-retry") {
        const image = button.previousElementSibling;
        image.src = image.src.split("?")[0] + "?retry=" + Date.now();
        image.hidden = false;
        button.hidden = true;
      }
    } catch (error) { toast(error.message); }
    finally { button.disabled = false; }
  }
  document.addEventListener("click", event => {
    const button = event.target.closest("[data-action]");
    if (button && !button.disabled) action(button).catch(error => toast(error.message));
  });
  document.addEventListener("error", event => {
    if (event.target.matches?.(".post-photo")) { event.target.hidden = true; event.target.nextElementSibling.hidden = false; }
  }, true);
  document.getElementById("refresh").addEventListener("click", () => { me = null; load(); });
  document.getElementById("profile").addEventListener("click", () => { if (me) profile(); });
  document.getElementById("close-sheet").addEventListener("click", () => sheet.close());
  sheet.addEventListener("close", () => { dialogVersion++; if (photoURL) { URL.revokeObjectURL(photoURL); photoURL = null; } });
  window.addEventListener("hashchange", () => { sheet.close(); window.scrollTo(0, 0); load({focus: true}); });
  window.addEventListener("pageshow", event => { if (event.persisted) { me = null; load(); } });
  document.addEventListener("change", async event => {
    const input = event.target;
    if (input.id === "show-name") {
      input.disabled = true;
      try {
        const data = await api("rankings/", {method: "POST", data: {show_my_name: input.checked}});
        ranking.show_my_name = data.show_my_name;
        toast("Ranking preference saved.");
      } catch (error) { input.checked = !input.checked; toast(error.message); }
      finally { input.disabled = false; }
    }
    if (input.id === "post-photo") {
      if (photoURL) URL.revokeObjectURL(photoURL);
      const preview = document.getElementById("photo-preview"), file = input.files[0];
      preview.hidden = true;
      if (file && file.size > 5 * 1024 * 1024) { input.value = ""; toast("Choose a photo smaller than 5 MB."); return; }
      if (file) { photoURL = URL.createObjectURL(file); preview.src = photoURL; preview.hidden = false; }
    }
  });

  sheetBody.addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.target, submit = form.querySelector('[type="submit"]'), id = Number(form.dataset.id);
    if (submit.disabled) return;
    const current = dialogVersion;
    submit.disabled = true;
    form.querySelector("[data-form-error]").hidden = true;
    try {
      if (form.id === "post-form") {
        const data = new FormData(form), file = data.get("image");
        const body = String(data.get("body")).trim();
        const hashtags = String(data.get("tags")).split(/[\s,]+/).filter(Boolean);
        if (!body && !file?.size) throw new Error("Write an update or choose a photo to share.");
        // Keep the same ID on a retry of the same draft, including a lost response.
        const fingerprint = JSON.stringify([body, hashtags, data.get("visibility"), file?.name, file?.size, file?.lastModified]);
        if (form.dataset.fingerprint !== fingerprint) {
          const bytes = crypto.getRandomValues(new Uint8Array(16));
          bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
          const hex = [...bytes].map(b => b.toString(16).padStart(2, "0")).join("");
          form.dataset.requestId = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
          form.dataset.fingerprint = fingerprint;
        }
        const payload = {body, hashtags, visibility: data.get("visibility"), client_request_id: form.dataset.requestId};
        let result;
        if (file?.size) {
          const upload = new FormData();
          for (const [key, value] of Object.entries(payload)) {
            if (Array.isArray(value)) value.forEach(tag => upload.append(key, tag));
            else upload.append(key, value);
          }
          upload.append("image", file);
          result = await api("community/posts/photos/", {method: "POST", data: upload});
        } else result = await api("community/posts/", {method: "POST", data: payload});
        if (current === dialogVersion) { sheet.close(); if (route === "community") await load(); }
        toast(result.status === "review" ? "Post submitted for staff review." : "Your update is live. Thanks for sharing!");
      } else if (form.id === "comment-form") {
        const comment = await api(`community/posts/${id}/comments/`, {method: "POST", data: {body: form.elements.body.value}});
        if (current === dialogVersion && sheet.open) {
          form.reset();
          // Append the confirmed comment immediately; a failed refresh must not invite a duplicate send.
          const list = document.getElementById("comments-list");
          if (!list.querySelector(".comment")) list.innerHTML = "";
          list.insertAdjacentHTML("beforeend", `<div class="comment"><strong>${esc(comment.author_name)}</strong><p class="comment-body">${esc(comment.body)}</p></div>`);
          const post = posts.find(p => p.id === id);
          if (post) replacePost({...post, comments_count: post.comments_count + 1});
        }
        toast("Comment added.");
      } else if (form.id === "report-form") {
        await api(`community/posts/${id}/report/`, {method: "POST", data: {reason: form.elements.reason.value}});
        if (current === dialogVersion) sheet.close();
        toast("Report sent to staff.");
      } else if (form.id === "delete-form") {
        await api(`community/posts/${id}/`, {method: "DELETE"});
        if (current === dialogVersion) { sheet.close(); if (route === "community") await load(); }
        toast("Post removed.");
      }
    } catch (error) { if (current === dialogVersion) formError(form, error); }
    finally { submit.disabled = false; }
  });

  let refreshingRanking = false;
  setInterval(async () => {
    if (document.hidden || route !== "rankings" || loading || refreshingRanking || sheet.open || document.getElementById("show-name")?.disabled) return;
    const current = version;
    refreshingRanking = true;
    try {
      const data = await api(`rankings/?board=${board}`, {signal: controller.signal});
      if (current === version && !document.getElementById("show-name")?.disabled) {
        const focused = document.activeElement;
        ranking = data;
        // Do not replace a keyboard user's focused control during a background refresh.
        if (focused === screen || !screen.contains(focused)) renderRankings();
      }
    } catch (error) { if (error.name !== "AbortError" && current === version) toast("Rankings couldn't refresh. Showing the last update."); }
    finally { refreshingRanking = false; }
  }, 30000);
  load();
})();
