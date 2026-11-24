document.querySelectorAll("[data-seek]").forEach((button) => {
  button.addEventListener("click", () => {
    const video = button.closest(".clip").querySelector("video");
    video.currentTime = Number(button.dataset.seek);
  });
});

const activeJobs = document.querySelector("[data-refresh]");
if (activeJobs) {
  window.setInterval(async () => {
    if (document.hidden || activeJobs.contains(document.activeElement)) return;
    try {
      const response = await fetch("/jobs", { cache: "no-store" });
      if (!response.ok) return;
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      const updated = page.querySelector("main > section");
      if (updated && activeJobs.innerHTML !== updated.innerHTML
          && !activeJobs.contains(document.activeElement)) {
        activeJobs.innerHTML = updated.innerHTML;
      }
    } catch {
      // Reattempt after a transient local disconnect.
    }
  }, 2500);
}

for (const editor of document.querySelectorAll("[data-source-editor]")) {
  const video = editor.querySelector("[data-edit-source]");
  if (!video) continue;
  const picker = editor.querySelector("[data-edit-window]");
  const absoluteTime = () => video.currentTime + Number(video.dataset.offset || 0);
  const canvas = editor.querySelector("[data-edit-crop]");
  const pen = canvas.getContext("2d");
  const framing = editor.querySelector("[data-edit-framing]");
  const cameraInput = editor.querySelector("[name=camera_crop]");
  const gameInput = editor.querySelector("[name=game_crop]");
  const hudInput = editor.querySelector("[name=hud_crop]");
  const cuts = editor.querySelector("[name=cuts]");
  const clock = editor.querySelector("[data-edit-time]");
  let until = null;
  let auditionEnded = false;

  function rows() {
    return cuts.value.split("\n").filter(line => line.trim()).map(line =>
      line.split("|").map(value => value.trim()));
  }
  function current(shots, time) {
    const at = shots.findIndex(shot => Number(shot[0]) <= time && time < Number(shot[1]));
    if (at >= 0) return at;
    const before = shots.findLastIndex(shot => Number(shot[0]) <= time);
    return Math.max(0, before);
  }
  function keys(shot) {
    return (shot?.[3] || "").split(";").filter(key => key.trim())
      .map(key => key.split(",").map(Number))
      .filter(key => key.length === 3 && key.every(Number.isFinite));
  }
  function center(shot, time) {
    const points = keys(shot);
    if (!points.length) return [.5, .5];
    const at = points.findIndex(point => point[0] > time);
    if (at === 0) return points[0].slice(1);
    if (at < 0) return points.at(-1).slice(1);
    const [a, x, y] = points[at - 1];
    const [b, nextX, nextY] = points[at];
    return [x + (nextX - x) * (time - a) / (b - a),
      y + (nextY - y) * (time - a) / (b - a)];
  }
  function rectangle(value) {
    if (!value.trim()) return null;
    const parts = value.split(",").map(Number);
    const [x, y, w, h] = parts;
    if (parts.length !== 4 || parts.some(n => !Number.isFinite(n) || n < 0) ||
        Math.min(w, h) < .035 || x + w > 1 || y + h > 1) return null;
    return parts;
  }
  function drawPanel(region, centerX, centerY, top, height) {
    const sourceWidth = video.videoWidth;
    const sourceHeight = video.videoHeight;
    const [x, y, w, h] = region.map((value, i) => value * (i % 2 ? sourceHeight : sourceWidth));
    const cropW = Math.min(w, h * canvas.width / height);
    const cropH = Math.min(h, w * height / canvas.width);
    const left = Math.max(x, Math.min(x + w - cropW, centerX * sourceWidth - cropW / 2));
    const upper = Math.max(y, Math.min(y + h - cropH, centerY * sourceHeight - cropH / 2));
    pen.drawImage(video, left, upper, cropW, cropH, 0, top, canvas.width, height);
  }
  function note(message) {
    if (framing.textContent !== message) framing.textContent = message;
  }
  function show() {
    const time = absoluteTime();
    if (until !== null && time >= until) {
      video.pause();
      until = null;
      auditionEnded = true;
    }
    clock.textContent = `${time.toFixed(3)} source seconds${auditionEnded ? " · audition ended" : ""}`;
    if (!video.videoWidth) return;
    const shots = rows();
    const shot = shots[current(shots, time)];
    pen.fillStyle = "#101215";
    pen.fillRect(0, 0, canvas.width, canvas.height);
    const camera = rectangle(cameraInput.value);
    if (!camera) {
      note("Enter a camera rectangle to preview it. The worker can search the edges for a camera when this field is blank.");
      return;
    }
    const [x, y, w, h] = camera;
    const override = rectangle(gameInput.value);
    const game = override || (x + w <= .40 ? [x + w, 0, 1 - x - w, 1] : x >= .60 ? [0, 0, x, 1] : null);
    if (!game || (gameInput.value.trim() && (!override || (game[0] + game[2] > x + .002 && game[0] < x + w - .002)))) {
      note("Set the gameplay bounds to a region beside the camera.");
      return;
    }
    const split = canvas.height * .35;
    drawPanel(camera, x + w / 2, y + h / 2, 0, split);
    const [cx, cy] = keys(shot).length ? center(shot, time) : [.5, .60];
    drawPanel(game, cx, cy, split, canvas.height - split);
    const hud = rectangle(hudInput.value);
    if (hud) {
      const [hx,hy,hw,hh] = hud.map((value,i) => value * (i%2 ? video.videoHeight : video.videoWidth));
      const scale = Math.min(51.5/hw,37/hh);
      pen.drawImage(video,hx,hy,hw,hh,237-hw*scale,split+72.75,hw*scale,hh*scale);
    }
    note("Camera crop " + Math.round(w * video.videoWidth) + " × " + Math.round(h * video.videoHeight) +
      " source pixels. Check the face and gameplay at the playhead.");
  }
  for (const event of ["seeked", "loadeddata", "timeupdate"]) video.addEventListener(event, show);
  for (const input of [cuts, cameraInput, gameInput, hudInput]) input.addEventListener("input", show);
  if (video.requestVideoFrameCallback) {
    const renderFrame = () => {
      show();
      video.requestVideoFrameCallback(renderFrame);
    };
    video.requestVideoFrameCallback(renderFrame);
  }

  function switchWindow(option) {
    video.pause();
    video.dataset.offset = option.dataset.start;
    video.dataset.start = option.dataset.start;
    video.src = option.value;
    video.load();
  }
  if (picker) picker.addEventListener("change", () => {
    until = null;
    switchWindow(picker.selectedOptions[0]);
  });
  async function seek(time) {
    if (picker) {
      const option = [...picker.options].find(w => time >= Number(w.dataset.start)-.001 && time < Number(w.dataset.end));
      if (!option) throw Error("Source window unavailable");
      if (option !== picker.selectedOptions[0]) {
        picker.value = option.value;
        switchWindow(option);
      }
    }
    if (video.readyState === 0) {
      video.preload = "metadata";
      await new Promise((resolve, reject) => {
        const cancel = new AbortController();
        const listener = {once: true, signal: cancel.signal};
        video.addEventListener("loadedmetadata", () => { cancel.abort(); resolve(); }, listener);
        video.addEventListener("error", () => { cancel.abort(); reject(); }, listener);
        video.load();
      });
    }
    video.currentTime = Math.max(0,time-Number(video.dataset.offset || 0));
  }

  for (const button of editor.querySelectorAll("[data-edit-command]")) {
    button.addEventListener("click", async () => {
      const shots = rows();
      const time = absoluteTime().toFixed(3);
      const index = current(shots, Number(time));
      const shot = shots[index];
      const command = button.dataset.editCommand;
      until = null;
      auditionEnded = false;
      if (command === "source") {
        try {
          await seek(Number(video.dataset.start));
        } catch {
          clock.textContent = "Source footage could not be loaded.";
        }
        return;
      }
      if (!shot) return;
      if (command === "audition") {
        try {
          await seek(Number(shot[0]));
          until = Number(shot[1]);
          await video.play();
        } catch {
          until = null;
          clock.textContent = "Source audition could not play.";
        }
        return;
      }
      if (command === "split") {
        if (Number(time) - Number(shot[0]) < .2 || Number(shot[1]) - Number(time) < .2) return;
        const points = keys(shot);
        const before = points.filter(point => point[0] < time);
        const after = points.filter(point => point[0] > time);
        if (points.length) {
          const cutCenter = [Number(time), ...center(shot, Number(time))];
          before.push(cutCenter);
          after.unshift(cutCenter);
        }
        shots.splice(index, 1,
          [shot[0], time, shot[2], before.map(p => p.join(",")).join("; "), shot[4] || ""],
          [time, shot[1], shot[2], after.map(p => p.join(",")).join("; ")]);
      } else {
        shot[command === "in" ? 0 : 1] = time;
        shot[3] = keys(shot).filter(p => p[0] >= Number(shot[0]) && p[0] <= Number(shot[1]))
          .map(p => p.join(",")).join("; ");
      }
      cuts.value = shots.map(parts => parts.join(" | ")).join("\n");
      show();
    });
  }
  video.addEventListener("pointerdown", event => {
    if (!event.shiftKey || !video.videoWidth) return;
    const shots = rows();
    const time = Number(absoluteTime().toFixed(3));
    const shot = shots[current(shots, time)];
    if (!shot || time < Number(shot[0]) || time >= Number(shot[1])) return;
    const bounds = video.getBoundingClientRect();
    const x = (event.clientX - bounds.left) / bounds.width;
    const y = (event.clientY - bounds.top) / bounds.height;
    const points = keys(shot).filter(p => p[0] !== time);
    points.push([time, Number(x.toFixed(4)), Number(y.toFixed(4))]);
    points.sort((a, b) => a[0] - b[0]);
    shot[2] = "focus";
    shot[3] = points.map(p => p.join(",")).join("; ");
    cuts.value = shots.map(parts => parts.join(" | ")).join("\n");
    show();
  });
}

const liveFactory = document.querySelector("[data-factory-refresh]");
if (liveFactory) {
  window.setInterval(async () => {
    if (document.hidden || liveFactory.contains(document.activeElement) || liveFactory.querySelector("details[open]")) return;
    try {
      const page = await fetch(location.href, {cache:"no-store"});
      if (!page.ok) return;
      const next = new DOMParser().parseFromString(await page.text(),"text/html").querySelector("[data-factory-refresh]");
      if (next && !liveFactory.contains(document.activeElement) && !liveFactory.querySelector("details[open]")) {
        liveFactory.innerHTML = next.innerHTML;
      }
    } catch {
      // Keep the last readable state on a local disconnect.
    }
  }, 6500);
}

const workspace = document.querySelector("[data-creator-workspace]");
if (workspace) {
  const posterObserver = new IntersectionObserver(entries => {
    for (const {target, isIntersecting} of entries) {
      if (!isIntersecting) continue;
      target.poster = target.dataset.workspacePoster;
      posterObserver.unobserve(target);
    }
  }, {rootMargin: "250px"});
  const posters = root => root.querySelectorAll("video[data-workspace-poster]:not([poster])")
    .forEach(video => posterObserver.observe(video));
  posters(workspace);
  const creator = workspace.querySelector("[data-creator-filter]");
  const status = workspace.querySelector("[data-stream-filter]");
  const memory = JSON.parse(sessionStorage.getItem("factory-view") || "{}");
  const reelMemory = memory.reels || {};
  const reelFilters = memory.reelFilters || {};
  const anchor = document.getElementById(location.hash.slice(1))?.closest("[data-stream]");
  const requested = anchor?.dataset.stream || workspace.dataset.selectedStream;
  const streams = [...workspace.querySelectorAll("[data-stream]")];
  const first = streams.find(stream => stream.dataset.category === "active") || streams[0];
  const explicit = requested && streams.find(stream => stream.dataset.stream === requested);
  const remember = () => {
    sessionStorage.setItem("factory-view", JSON.stringify({
      creator: creator.value, status: status.value, reels: reelMemory, reelFilters,
      open: streams.filter(stream => stream.open).map(stream => stream.dataset.stream)
    }));
  };
  creator.value = memory.creator || "";
  status.value = memory.status || "";
  if (explicit && creator.value && creator.value !== explicit.closest("[data-creator]").dataset.creator) creator.value = "";
  if (explicit && status.value && status.value !== explicit.dataset.category) status.value = "";
  for (const stream of streams) {
    stream.open = explicit ? stream === explicit : memory.open ? memory.open.includes(stream.dataset.stream) : stream === first;
    stream.addEventListener("toggle", () => {
      if (stream.open) {
        const params = new URLSearchParams({stream:stream.dataset.stream});
        if (stream.dataset.attempt) params.set("attempt",stream.dataset.attempt);
        const back = "?" + params;
        sessionStorage.setItem("factory-return",back);
        for (const link of document.querySelectorAll("header nav a")) {
          if (link.pathname !== "/factory") link.search = back;
        }
      }
      remember();
    });
  }
  function filter() {
    let visible = 0;
    for (const group of workspace.querySelectorAll("[data-creator]")) {
      let matches = 0;
      for (const stream of group.querySelectorAll("[data-stream]")) {
        stream.hidden = (creator.value && group.dataset.creator !== creator.value) ||
          (status.value && stream.dataset.category !== status.value);
        const shortcut = workspace.querySelector('[data-quick-stream="' + stream.dataset.stream + '"]');
        if (shortcut) shortcut.hidden = stream.hidden;
        matches += Number(!stream.hidden);
      }
      group.hidden = !matches;
      visible += matches;
    }
    workspace.querySelector("[data-no-streams]").hidden = visible > 0 || (!creator.value && !status.value);
  }
  for (const control of [creator,status]) control.addEventListener("change", () => { filter(); remember(); });
  filter();
  if (explicit && anchor) requestAnimationFrame(() => anchor.scrollIntoView({block:"nearest"}));
  function displayReels(gallery, selected) {
    const filter = gallery.querySelector("[data-reel-filter]");
    if (!filter) return;
    const stream = gallery.closest("[data-stream]");
    const picks = [...gallery.querySelectorAll("[data-reel-pick]")];
    const match = picks.filter(pick => !filter.value || pick.dataset.reelState === filter.value);
    const more = gallery.querySelector("[data-reel-more]");
    selected = [selected,reelMemory[stream.dataset.stream]].find(id => match.some(pick => pick.dataset.reelPick === id))
      || match[0]?.dataset.reelPick;
    if (selected) reelMemory[stream.dataset.stream] = selected;
    for (const pick of picks) {
      const position = match.indexOf(pick);
      pick.hidden = position < 0 || (position >= 4 && more?.getAttribute("aria-expanded") !== "true");
      pick.setAttribute("aria-pressed", String(selected === pick.dataset.reelPick));
    }
    if (more) more.hidden = match.length <= 4;
    for (const card of gallery.querySelectorAll("[data-reel-card]")) {
      if (card.id !== selected && !card.hidden) for (const video of card.querySelectorAll("video")) video.pause();
      card.hidden = card.id !== selected;
    }
    gallery.querySelector("[data-no-reels]").hidden = match.length > 0;
    const index = match.findIndex(pick => pick.dataset.reelPick === selected);
    const [prev,next] = gallery.querySelectorAll("[data-reel-step]");
    prev.disabled = index <= 0;
    next.disabled = index < 0 || index === match.length - 1;
    remember();
  }
  for (const gallery of workspace.querySelectorAll("[data-workspace-gallery]")) {
    const filter = gallery.querySelector("[data-reel-filter]");
    if (filter && ["unreviewed","ready","rework"].includes(workspace.dataset.initialReview)) {
      filter.value = workspace.dataset.initialReview;
    } else if (filter) {
      filter.value = reelFilters[gallery.closest("[data-stream]").dataset.stream] || "";
    }
    const direct = location.hash.startsWith("#reel-") ? location.hash.slice(1) : null;
    displayReels(gallery,direct || reelMemory[gallery.closest("[data-stream]").dataset.stream] ||
      gallery.querySelector("[data-reel-card]:not([hidden])")?.id);
  }
  workspace.addEventListener("change", event => {
    if (!event.target.matches("[data-reel-filter]")) return;
    reelFilters[event.target.closest("[data-stream]").dataset.stream] = event.target.value;
    displayReels(event.target.closest("[data-workspace-gallery]"));
  });
  workspace.addEventListener("click", async event => {
    const shortcut = event.target.closest("[data-quick-stream]");
    if (shortcut && event.button===0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {
      const stream = document.getElementById("stream-" + shortcut.dataset.quickStream);
      event.preventDefault();
      stream.open = true;
      stream.querySelector("summary").focus({preventScroll:true});
      stream.scrollIntoView({block:"start"});
    }
    const reelAction = event.target.closest("[data-reel-pick], [data-reel-more], [data-reel-step]");
    if (reelAction) {
      const gallery = reelAction.closest("[data-workspace-gallery]");
      let pick = reelAction.dataset.reelPick;
      if (reelAction.hasAttribute("data-reel-more")) {
        const open = reelAction.getAttribute("aria-expanded") === "true";
        reelAction.setAttribute("aria-expanded",String(!open));
        reelAction.textContent = open ? "View all " + gallery.querySelectorAll("[data-reel-pick]").length : "Show fewer";
      } else if (reelAction.dataset.reelStep) {
        const value = gallery.querySelector("[data-reel-filter]").value;
        const options = [...gallery.querySelectorAll("[data-reel-pick]")]
          .filter(p => !value || p.dataset.reelState === value);
        const index = options.findIndex(p => p.getAttribute("aria-pressed") === "true");
        pick = options[index+Number(reelAction.dataset.reelStep)]?.dataset.reelPick;
      }
      displayReels(gallery,pick);
    }
    if (event.target.closest("[data-collapse-streams]")) {
      for (const stream of workspace.querySelectorAll("[data-stream]:not([data-category=active])")) {
        if (!stream.querySelector("video:not([preload=none])") &&
            ![...stream.querySelectorAll("video")].some(v => !v.paused)) stream.open = false;
      }
    }
    const sourceAction = event.target.closest("[data-source-at], [data-source-play]");
    if (!sourceAction) return;
    const video = sourceAction.closest("[data-stream]").querySelector("[data-stream-source]");
    if (!video) return;
    event.preventDefault();
    if (sourceAction.dataset.sourceAt) {
      video.currentTime = Number(sourceAction.dataset.sourceAt);
      video.scrollIntoView({block:"nearest"});
      video.focus({preventScroll:true});
    }
    try {
      await video.play();
    } catch {
      video.focus();
    }
  });
  let refreshing = false;
  window.setInterval(async () => {
    if (document.hidden || refreshing) return;
    refreshing = true;
    try {
      const response = await fetch(location.href, {cache:"no-store"});
      if (!response.ok) return;
      const next = new DOMParser().parseFromString(await response.text(),"text/html");
      for (const stream of workspace.querySelectorAll("[data-stream]")) {
        const replacement = next.getElementById(stream.id);
        if (!replacement) continue;
        stream.dataset.category = replacement.dataset.category;
        const quick = workspace.querySelector('[data-quick-stream="' + stream.dataset.stream + '"] [data-quick-status]');
        const nextStatus = replacement.querySelector("[data-workspace-status]");
        if (quick && nextStatus && quick.innerHTML !== nextStatus.innerHTML) quick.innerHTML = nextStatus.innerHTML;
        for (const selector of ["[data-workspace-status]", ".workspace-consequence", "[data-workspace-source]",
                                "[data-workspace-processing]", "[data-workspace-gallery]", ".workspace-history"]) {
          const now = stream.querySelector(selector);
          const later = replacement.querySelector(selector);
          const processing = selector === "[data-workspace-processing]";
          if (!now || !later || now.contains(document.activeElement) || now.matches("details[open]") ||
              (!processing && now.querySelector("details[open]")) ||
              now.querySelector("[data-reel-more][aria-expanded=true]") ||
              now.querySelector("[data-reel-filter]:has(option:checked:not([value='']))") ||
              [...now.querySelectorAll("video")].some(video => video.readyState > 0 || !video.paused)) continue;
          const normalizePosters = html => html.replace(/ poster="[^"]*"/g, "");
          const expanded = processing ? new Set([...now.querySelectorAll("[data-step][open]")]
            .map(step => step.dataset.step)) : new Set();
          for (const step of later.querySelectorAll("[data-step]")) step.open = expanded.has(step.dataset.step);
          if (now.className === later.className && normalizePosters(now.innerHTML) === normalizePosters(later.innerHTML)) continue;
          now.className = later.className;
          now.innerHTML = later.innerHTML;
          posters(now);
          if (selector === "[data-workspace-gallery]") displayReels(now);
        }
      }
      filter();
    } catch {
      // A local network interruption leaves the last readable state in place.
    } finally {
      refreshing = false;
    }
  }, 6500);
}

for (const cue of document.querySelectorAll("[data-cue-original][data-cue-version]")) {
  cue.addEventListener("click", () => {
    for (const [label, time] of [["Original portrait", cue.dataset.cueOriginal], ["Version portrait", cue.dataset.cueVersion]]) {
      const video = document.querySelector(`video[aria-label="${label}"]`);
      video.pause();
      if (!video.readyState) {
        video.addEventListener("loadedmetadata", () => { video.currentTime = Number(time); }, {once:true});
        video.preload = "metadata";
        video.load();
      } else {
        video.currentTime = Number(time);
      }
    }
  });
}
for (const menu of document.querySelectorAll(".workspace-menu")) {
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && menu.open) {
      menu.open = false;
      menu.querySelector("summary").focus();
      event.preventDefault();
    }
  });
  document.addEventListener("click", event => {
    if (menu.open && !menu.contains(event.target)) menu.open = false;
  });
}
const workspaceContext = new URLSearchParams(location.search);
if (workspaceContext.get("stream")) {
  sessionStorage.setItem("factory-return", "?" + new URLSearchParams({
    stream:workspaceContext.get("stream"), ...(workspaceContext.get("attempt") ? {attempt:workspaceContext.get("attempt")} : {})
  }));
}
for (const link of document.querySelectorAll("header a[href='/factory']")) {
  if (link.closest("nav")) link.href = "/factory" + (sessionStorage.getItem("factory-return") || "");
}
function revealTarget() {
  const target = document.getElementById(location.hash.slice(1));
  if (!target) return;
  for (let ancestor = target; ancestor; ancestor = ancestor.parentElement) {
    if (ancestor.tagName === "DETAILS") ancestor.open = true;
  }
}
window.addEventListener("hashchange", revealTarget);
revealTarget();
const rosterSearch = document.querySelector("[data-roster-search]");
if (rosterSearch) rosterSearch.addEventListener("input", () => {
  for (const creator of document.querySelectorAll("[data-roster-creator]")) {
    creator.hidden = !creator.textContent.toLowerCase().includes(rosterSearch.value.trim().toLowerCase());
  }
});
for (const intake of document.querySelectorAll("[data-recording-intake]")) {
  intake.querySelector("[data-recording-upload]").addEventListener("click", async () => {
    const file = intake.querySelector("[data-recording-file]").files[0];
    const status = intake.querySelector("[data-recording-status]");
    if (!file) {
      status.textContent = "Choose a video file.";
      return;
    }
    const form = intake.querySelector("form");
    status.textContent = "Receiving recording on this machine…";
    try {
      const response = await fetch("/sources/recording-upload", {method:"POST", body:file,headers:{
        "Content-Type":"application/octet-stream","X-CSRF":form.elements.csrf.value
      }});
      if (!response.ok) throw new Error(await response.text());
      form.elements.link.value = (await response.json()).path;
      if (!form.elements.title.value) form.elements.title.value = file.name;
      form.requestSubmit();
    } catch (error) {
      status.textContent = error.message || "The file transfer failed.";
    }
  });
}
const fonts = {};
async function drawTemplate(canvas, fields) {
  const ctx = canvas.getContext("2d");
  const scale = canvas.width/1080;
  const y = Number(fields.y)*scale;
  const x = Number(fields.x)*scale;
  const backdrop = ctx.createLinearGradient(0,0,canvas.width,canvas.height);
  backdrop.addColorStop(0,"#3b4b65");
  backdrop.addColorStop(.35,"#27374b");
  backdrop.addColorStop(.352,"#314237");
  backdrop.addColorStop(1,"#132720");
  ctx.fillStyle = backdrop;
  ctx.fillRect(0,0,canvas.width,canvas.height);
  ctx.save();
  ctx.strokeStyle = "#ffffff25";
  ctx.setLineDash([4,5]);
  ctx.beginPath();ctx.moveTo(0,672*scale);ctx.lineTo(canvas.width,672*scale);ctx.stroke();
  ctx.fillStyle="#ffffffa0";
  ctx.font="12px system-ui";
  ctx.fillText("Presenter · 35%",12,25);
  ctx.fillText("Gameplay · 65%",12,canvas.height-25);
  ctx.restore();
  const family = "preview-" + fields.font;
  if (!fonts[fields.font]) {
    const font = new FontFace(family,"url(/templates/asset/font/" + encodeURIComponent(fields.font) + ")");
    fonts[fields.font] = font.load().then(value=>document.fonts.add(value)).catch(()=>{});
  }
  const stamp = JSON.stringify(fields);
  canvas.dataset.previewStamp = stamp;
  await fonts[fields.font];
  if (canvas.dataset.previewStamp !== stamp) return;
  const size = Number(fields.size)*scale;
  ctx.font = size + "px " + family + ", system-ui";
  const width = Math.min(canvas.width-32,ctx.measureText(fields.text).width+22);
  ctx.fillStyle = fields.backing;
  ctx.fillRect(x-width/2,y-size*.9,width,size+10);
  ctx.fillStyle = fields.fill;
  ctx.textAlign = "center";
  ctx.fillText(fields.text,x,y,width-16);
}
for (const canvas of document.querySelectorAll("[data-template-preview]")) {
  const draft = JSON.parse(canvas.dataset.templatePreview);
  drawTemplate(canvas,draft);
  const form = canvas.closest(".template-designer")?.querySelector(".template-fields");
  form?.addEventListener("input", () => drawTemplate(canvas,{...draft,...Object.fromEntries(new FormData(form))}));
}
for (const form of document.querySelectorAll(".control-fields")) {
  const key = "controls-draft:" + form.getAttribute("action");
  const version = form.elements.version.value;
  const prior = JSON.parse(sessionStorage.getItem(key) || "null");
  if (prior?.version === version) {
    for (const input of form.querySelectorAll("input:not([type=hidden]),select,textarea")) {
      if (input.type === "checkbox") input.checked = prior.fields[input.name] === "on";
      else if (Object.hasOwn(prior.fields,input.name)) input.value = prior.fields[input.name];
    }
  }
  form.addEventListener("input",()=>{
    const fields = new FormData(form);
    fields.delete("csrf");
    sessionStorage.setItem(key,JSON.stringify({version,fields:Object.fromEntries(fields)}));
  });
}
for (const selection of document.querySelectorAll("[data-template-choices]")) {
  const form = selection.closest("form");
  const checks = [...selection.querySelectorAll("[data-template-choice]")];
  const hidden = form.elements.ids;
  const saved = hidden.value.split(",").map(value=>value.trim());
  for (const check of checks) check.checked = saved.includes(check.value);
  selection.addEventListener("change",event=>{
    if (form.elements.mode.value === "fixed" && event.target.checked) {
      for (const check of checks) if (check !== event.target) check.checked = false;
    }
    hidden.value = checks.filter(check=>check.checked).map(check=>check.value).join(",");
    hidden.dispatchEvent(new Event("input",{bubbles:true}));
  });
}

const currentFactory = document.querySelector("[data-factory-current]");
if (currentFactory?.querySelector('[data-busy="1"]')) {
  async function refreshCurrentFactory() {
    try {
      const response = await fetch("/factory", {cache:"no-store"});
      if (!response.ok) return;
      const page = new DOMParser().parseFromString(await response.text(),"text/html");
      const incoming = page.querySelector("[data-factory-current]");
      if (!incoming) return;
      for (const run of incoming.querySelectorAll("[data-current-run]")) {
        const prior = document.getElementById(run.id);
        if (!prior) continue;
        run.open = prior.open;
        const players = new Map([...prior.querySelectorAll("video")].map(video=>[video.getAttribute("src"),video]));
        for (const video of run.querySelectorAll("video")) {
          if (players.has(video.getAttribute("src"))) video.replaceWith(players.get(video.getAttribute("src")));
        }
        prior.replaceWith(run);
      }
    } finally {
      if (currentFactory.querySelector('[data-busy="1"]')) setTimeout(refreshCurrentFactory,5000);
    }
  }
  setTimeout(refreshCurrentFactory,5000);
}

const bufferCount = document.querySelector("[data-buffer-count]");
if (bufferCount) {
  const detail = document.querySelector("[data-buffer-detail]");
  async function refreshBuffer() {
    try {
      if (document.hidden) return;
      const response = await fetch("/storage/buffer", {cache:"no-store"});
      if (!response.ok) return;
      const {count, detail: status} = await response.json();
      bufferCount.textContent = `${count} ${count === 1 ? "reel" : "reels"} in buffer`;
      detail.textContent = status;
    } catch {
      // Keep the last count while the Factory is reconnecting.
    } finally {
      setTimeout(refreshBuffer,6000);
    }
  }
  setTimeout(refreshBuffer,6000);
}


function updateReplayCountdown(panel) {
  if (!panel.dataset.deleteAt) return;
  const minutes = Math.max(0, Math.ceil((Number(panel.dataset.deleteAt) - performance.now()) / 60000));
  panel.querySelector("[data-replay-countdown]").textContent =
    minutes ? `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m until deletion` : "Deletion due";
}

async function refreshReplayProgress() {
  if (document.hidden) return;
  await Promise.all([...document.querySelectorAll("[data-replay-controls]")].map(async controls => {
    try {
      const response = await fetch(`/jobs/source/${controls.dataset.replayControls}/replay-progress`, {cache:"no-store"});
      if (!response.ok) return;
      const state = await response.json();
      const cached = state.state === "verified" && state.expires;
      const countdown = controls.querySelector("[data-replay-countdown]");
      if (cached) {
        controls.dataset.deleteAt = performance.now() + 1000 * (state.expires - state.server_time);
        updateReplayCountdown(controls);
      } else {
        delete controls.dataset.deleteAt;
        countdown.textContent =
          ({expired:"Recording deleted",expiring:"Removing recording",verified:"Attached recording"})[state.state]
          || "Waiting for the recording";
      }
      controls.querySelector("[data-replay-extension]").hidden = !cached || state.expires <= state.server_time;
      const toggle = controls.querySelector("[data-replay-toggle]");
      if (toggle) toggle.hidden = ["expired","expiring"].includes(state.state);
      const panel = controls.closest("[data-replay-progress]");
      if (!panel) return;
      function progress(selector,value,total) {
        const bar = panel.querySelector(selector);
        if (total > 0) {
          bar.max = total;
          bar.value = Math.min(value,total);
        } else {
          bar.removeAttribute("value");
        }
      }
      progress("[data-download-progress]",state.bytes,state.expected);
      progress("[data-transcript-progress]",state.transcribed,state.duration);
      const gb = bytes => (bytes/1e9).toFixed(2) + " GB";
      const minutes = seconds => Math.floor(seconds/60) + " min";
      const labels = {queued:"Waiting to start",submitting:"Submitting",pending:"Provider preparing the recording",
        verifying:"Verifying the recording",verified:"Cached",expired:"Cache expired",expiring:"Removing cache",
        held:"Transfer held",failed:"Provider failed",rejected:"Provider rejected"};
      const tally = state.expected ? `${gb(state.bytes)} / ${gb(state.expected)}` : gb(state.bytes);
      panel.querySelector("[data-download-count]").textContent =
        [labels[state.state],state.bytes ? tally : ""].filter(Boolean).join(" · ");
      panel.querySelector("[data-transcript-count]").textContent = state.duration
        ? `${minutes(state.transcribed)} / ${minutes(state.duration)}` : "Waiting for the recording";
    } catch {
      // Keep the last known deadline and progress while disconnected.
    }
  }));
}

if (document.querySelector("[data-replay-controls]")) {
  refreshReplayProgress();
  setInterval(refreshReplayProgress,5000);
  setInterval(() => document.querySelectorAll("[data-replay-controls]").forEach(updateReplayCountdown),1000);
}

let jobBoard = document.querySelector("[data-operator-jobs]");
if (jobBoard) {
  const logRequests = new WeakSet();
  async function refreshJobLog(panel) {
    if (document.hidden || !panel.open || !panel.getClientRects().length || logRequests.has(panel)) return;
    logRequests.add(panel);
    const display = panel.querySelector("pre");
    try {
      const after = panel.dataset.logCursor || "0";
      const response = await fetch(`/jobs/${panel.dataset.jobLog}/live-log?after=${after}`, {cache:"no-store"});
      if (!response.ok) throw new Error("Log unavailable");
      const value = await response.text();
      panel.dataset.logCursor = response.headers.get("X-Factory-Log-Cursor") || "0";
      if (!value) {
        if (after === "0") display.textContent = "Waiting for worker events…";
        return;
      }
      const atBottom = display.scrollHeight - display.scrollTop - display.clientHeight < 24;
      const lines = (after === "0" ? value : display.textContent + "\n" + value).split("\n");
      display.textContent = lines.slice(-500).join("\n");
      if (atBottom) display.scrollTop = display.scrollHeight;
    } catch {
      if (display.textContent === "Loading…") display.textContent = "Waiting for server logs…";
    } finally {
      logRequests.delete(panel);
    }
  }
  function loadJobMedia() {
    for (const media of jobBoard.querySelectorAll("[data-job-media]:not([src])")) {
      let section = media.closest("details");
      while (section?.open) section = section.parentElement.closest("details");
      if (!section) media.src = media.dataset.jobMedia;
    }
  }
  function showJobMedia() {
    jobBoard.addEventListener("toggle", loadJobMedia, true);
    jobBoard.addEventListener("toggle", event => {
      if (event.target.matches("[data-job-log][open]")) refreshJobLog(event.target);
    }, true);
    loadJobMedia();
  }
  async function refreshJobs() {
    try {
      if (document.hidden) return;
      const response = await fetch(location.pathname + location.search, {cache:"no-store"});
      if (!response.ok) return;
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      const incoming = page.querySelector("[data-operator-jobs]");
      if (!incoming || incoming.innerHTML === jobBoard.innerHTML) return;
      if (jobBoard.contains(document.activeElement) ||
          [...jobBoard.querySelectorAll("audio,video")].some(media => !media.paused)) {
        for (const next of incoming.querySelectorAll(".operator-story-progress")) {
          const current = document.getElementById(next.id);
          if (current) current.replaceWith(next);
          else {
            const stage = document.getElementById(next.parentElement.id);
            if (stage) stage.append(next);
          }
        }
        return;
      }
      const disclosure = new Map([...jobBoard.querySelectorAll("details[id]")].map(node => [node.id,node.open]));
      for (const node of incoming.querySelectorAll("details[id]")) {
        if (disclosure.has(node.id)) node.open = disclosure.get(node.id);
        if (node.matches("[data-job-log][open]")) {
          const prior = document.getElementById(node.id);
          if (prior) node.replaceWith(prior);
        }
      }
      document.querySelector("[data-job-count]").textContent = page.querySelector("[data-job-count]").textContent;
      jobBoard.replaceWith(incoming);
      jobBoard = incoming;
      showJobMedia();
    } catch {
      // Keep the latest saved view if the local portal is restarting.
    } finally {
      if (jobBoard.dataset.busy === "1") setTimeout(refreshJobs,4000);
    }
  }
  showJobMedia();
  setInterval(() => jobBoard.querySelectorAll("[data-job-log][open]").forEach(refreshJobLog), 1500);
  if (jobBoard.dataset.busy === "1") setTimeout(refreshJobs,4000);
}
