"use strict";

const $ = (selector) => document.querySelector(selector);
const POSTURES = {
  standing: "站着", sitting: "坐着", lying: "躺着", squatting: "蹲着",
  curled: "蜷缩", unknown: "无法判断", other: "其他",
};
const ACTIONS = {
  none: "无明显动作", raised_hand: "举手", waving: "挥手",
  greeting: "打招呼", unknown: "无法判断", other: "其他",
};
const STATUSES = { pending: "待审核", confirmed: "已确认", excluded: "已排除" };
const review = {
  csrf: "", filter: "all", query: "", identity: "", posture: "", labelSource: "effective",
  categories: [], selected: new Map(), selectionConflict: false, deleteCandidates: [], deleteScope: "labels",
  page: 1, pages: 0, total: 0, items: [],
  current: null, people: [], notes: "", baseline: "", dirty: false,
  loading: false, listing: false, saving: false, batching: false, expired: false, conflict: false,
  imageMode: "annotated", pendingAction: null, listGeneration: 0, detailGeneration: 0,
};
let toastTimer;

class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function notify(message) {
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("#toast").hidden = true; }, 6000);
}

function showNotice(selector, message, kind = "") {
  const node = $(selector);
  node.replaceChildren(document.createTextNode(message));
  node.className = "notice" + (kind ? " " + kind : "");
  node.hidden = !message;
}

function sessionExpired() {
  if (review.expired) return;
  review.expired = true;
  const node = $("#page-notice");
  node.className = "notice error";
  node.replaceChildren(document.createTextNode("登录已失效，未保存的修改仍保留在本页。"));
  const link = element("a", "", "重新登录");
  link.href = "/";
  link.target = "_blank";
  link.rel = "noopener";
  node.append(document.createTextNode(" "), link, document.createTextNode("后返回本页重新读取。"));
  node.hidden = false;
  updateControls();
}

async function request(path, options = {}) {
  if (review.expired) throw new ApiError("登录已失效，请重新登录。", 401);
  let response;
  try {
    response = await fetch(path, { credentials: "same-origin", cache: "no-store", ...options });
  } catch (_) {
    throw new ApiError("无法连接老大 Mini，请检查网络后重试。", 0);
  }
  if (response.status === 401) {
    sessionExpired();
    throw new ApiError("登录已失效，请重新登录。", 401);
  }
  if (!response.ok) {
    let message = "读取失败，请稍后重试。";
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
      else if (response.status === 422) message = "标注内容不符合要求，请检查人物名称和分类。";
    } catch (_) { /* A proxy error need not contain JSON. */ }
    throw new ApiError(message, response.status);
  }
  return response;
}

async function json(path, options) {
  const response = await request(path, options);
  try { return await response.json(); }
  catch (_) { throw new ApiError("收到的照片信息不完整，请重新读取。", 0); }
}

function safeImageURL(value) {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value, location.origin);
    if (url.origin !== location.origin || !url.pathname.startsWith("/api/v1/photos/")) return null;
    return url.pathname + url.search;
  } catch (_) { return null; }
}

function formatTime(value, compact = false) {
  if (!value) return "采集时间未记录";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  if (!Number.isFinite(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Singapore", month: "2-digit", day: "2-digit",
    ...(compact ? {} : { year: "numeric" }), hour: "2-digit", minute: "2-digit",
    ...(compact ? {} : { second: "2-digit" }), hour12: false,
  }).format(date) + (compact ? "" : " · UTC+8");
}

function numberText(value) {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(3) : null;
}

function statusOf(item) {
  const status = item.annotation?.status;
  return Object.hasOwn(STATUSES, status) ? status : "pending";
}

function peopleOf(item) {
  return Array.isArray(item.annotation?.people) ? item.annotation.people :
    Array.isArray(item.predictions) ? item.predictions : [];
}

function revisionOf(item) {
  return Number.isSafeInteger(item.revision) ? item.revision : item.annotation?.revision || 0;
}

function selectionEntry(item) {
  return { revision: revisionOf(item), status: statusOf(item), hasAnnotation: !!item.annotation };
}

function selectIdentity(identity) {
  guarded(() => {
    review.identity = identity;
    if (identity) { review.labelSource = "manual"; review.filter = "confirmed"; }
    renderFilterControls(); loadList(1);
  });
  renderFilterControls();
}

function renderFilterControls() {
  $("#identity-filter").value = review.identity;
  $("#posture-filter").value = review.posture;
  $("#label-source").value = review.labelSource;
  for (const button of document.querySelectorAll("[data-category]")) {
    button.setAttribute("aria-pressed", String(button.dataset.category === review.identity && review.labelSource === "manual"));
  }
  for (const button of document.querySelectorAll("[data-filter]")) {
    button.setAttribute("aria-pressed", String(button.dataset.filter === review.filter));
  }
}

function renderCategories(categories) {
  review.categories = Array.isArray(categories) ? categories.filter((item) =>
    typeof item.identity === "string" && Number.isSafeInteger(item.count)) : [];
  const counts = new Map(review.categories.map((item) => [item.identity, item.count]));
  const identities = ["儿子", "爸爸", "妈妈", ...review.categories.map((item) => item.identity)];
  if (review.identity) identities.push(review.identity);
  const options = document.createDocumentFragment();
  const all = element("option", "", "全部人物"); all.value = ""; options.append(all);
  for (const identity of new Set(identities)) {
    const option = element("option", "", identity + "（" + (counts.get(identity) || 0) + "）");
    option.value = identity; options.append(option);
  }
  $("#identity-filter").replaceChildren(options);
  const buttons = document.createDocumentFragment();
  for (const identity of ["儿子", "爸爸", "妈妈"]) {
    const button = element("button", "", identity);
    button.type = "button"; button.dataset.category = identity;
    button.append(element("span", "", String(counts.get(identity) || 0)));
    button.setAttribute("aria-label", identity + "，人工确认 " + (counts.get(identity) || 0) + " 张照片");
    button.addEventListener("click", () => selectIdentity(identity));
    buttons.append(button);
  }
  $("#category-buttons").replaceChildren(buttons);
  renderFilterControls();
}

function renderSelection() {
  $("#selection-count").textContent = "已选 " + review.selected.size + " 张";
  const count = review.items.filter((item) => review.selected.has(item.id)).length;
  $("#select-page").checked = review.items.length > 0 && count === review.items.length;
  $("#select-page").indeterminate = count > 0 && count < review.items.length;
  $("#selection-conflict").hidden = !review.selectionConflict;
  for (const checkbox of document.querySelectorAll(".photo-select")) {
    const selected = review.selected.has(checkbox.dataset.photoId);
    checkbox.checked = selected;
    checkbox.closest(".photo-card").classList.toggle("is-selected", selected);
  }
  updateControls();
}

function toggleSelection(item, selected) {
  if (selected && !review.selected.has(item.id)) {
    if (review.selected.size >= 200) { notify("每次最多选择 200 张，请先导出或清除部分选择。" ); return; }
    review.selected.set(item.id, selectionEntry(item));
  } else if (!selected) review.selected.delete(item.id);
  if (!review.selected.size) review.selectionConflict = false;
  renderSelection();
}

function renderStats(stats = {}) {
  for (const name of ["total", "pending", "confirmed", "excluded"]) {
    $("#stat-" + name).textContent = Number.isSafeInteger(stats[name]) && stats[name] >= 0 ? stats[name] : "—";
  }
  $("#original-count").textContent = Number.isSafeInteger(stats.originals) ?
    stats.originals + " 张保留无标注原片" : "无标注原片状态待确认";
}

function renderList() {
  const fragment = document.createDocumentFragment();
  for (const item of review.items) {
    const card = element("article", "photo-card");
    card.dataset.photoId = item.id;
    card.setAttribute("aria-current", String(item.id === review.current?.id));
    const selector = element("label", "photo-select-label");
    const checkbox = element("input", "photo-select");
    checkbox.type = "checkbox"; checkbox.dataset.photoId = item.id;
    checkbox.setAttribute("aria-label", "选择 " + formatTime(item.sampled_at) + " 的照片");
    checkbox.addEventListener("change", () => toggleSelection(item, checkbox.checked));
    selector.append(checkbox);
    const open = element("button", "photo-open"); open.type = "button";
    const img = element("img");
    const imageURL = safeImageURL(item.image_url);
    if (imageURL) img.src = imageURL;
    img.alt = "";
    img.loading = "lazy";
    img.decoding = "async";
    const description = element("span", "photo-card-text");
    const people = peopleOf(item);
    const names = people.map((person) => person.identity || "未确认");
    const title = names.length ? [...new Set(names)].join("、") : "无人标注";
    description.append(element("strong", "", title));
    const categories = people.map((person) =>
      (POSTURES[person.posture] || person.posture_label || "无法判断") + " · " +
      (ACTIONS[person.action] || person.action_label || "无法判断"));
    description.append(element("span", "", categories.length ? [...new Set(categories)].join(" / ") : "没有人物分类"));
    const time = element("time", "", formatTime(item.sampled_at, true));
    if (typeof item.sampled_at === "string") time.dateTime = item.sampled_at;
    description.append(time);
    const status = statusOf(item);
    description.append(element("span", "status-tag " + status, STATUSES[status]));
    open.append(img, description);
    open.setAttribute("aria-label", title + "，" + formatTime(item.sampled_at) + "，" + STATUSES[status]);
    open.addEventListener("click", () => guarded(() => loadDetail(item.id, true)));
    card.append(selector, open);
    fragment.append(card);
  }
  $("#photo-list").replaceChildren(fragment);
  $("#list-empty").hidden = review.items.length > 0;
  $("#empty-retry").hidden = true;
  $("#empty-title").textContent = review.total === 0 && review.query ? "没有找到匹配的照片" :
    review.filter === "pending" ? "暂时没有待审核照片" : "这里还没有照片";
  $("#empty-description").textContent = review.query ? "试试其他人物名称，或清空搜索查看。" :
    review.filter === "pending" ? "可查看已确认的照片，或等待老三采集新画面。" :
    "切换审核状态，查看其他采集记录。";
  $("#list-count").textContent = "共 " + review.total + " 张" + (review.query ? " · 搜索结果" : "");
  $("#page-indicator").textContent = review.pages ? review.page + " / " + review.pages : "0 / 0";
  renderFilterControls();
  renderSelection();
  updateControls();
  updatePhotoNavigation();
}

async function loadList(page = review.page, options = {}) {
  const generation = ++review.listGeneration;
  review.listing = true;
  $("#photo-list").setAttribute("aria-busy", "true");
  updateControls();
  try {
    const params = new URLSearchParams({ page: String(page), limit: "24", status: review.filter, q: review.query,
      identity: review.identity, posture: review.posture, label_source: review.labelSource });
    const data = await json("/api/v1/photos?" + params);
    if (generation !== review.listGeneration) return false;
    review.items = Array.isArray(data.items) ? data.items : [];
    review.page = Number.isSafeInteger(data.page) ? data.page : page;
    review.pages = Number.isSafeInteger(data.pages) ? data.pages : 0;
    review.total = Number.isSafeInteger(data.total) ? data.total : 0;
    renderStats(data.stats);
    renderCategories(data.categories);
    if (review.page > review.pages && review.pages > 0 && !options.bounded) {
      return await loadList(review.pages, { bounded: true });
    }
    renderList();
    if (!review.expired) showNotice("#page-notice", "");
    return true;
  } catch (error) {
    if (generation !== review.listGeneration) return false;
    if (!review.expired) showNotice("#page-notice", error.message, "error");
    if (!review.items.length) {
      $("#list-empty").hidden = false;
      $("#empty-title").textContent = "照片列表暂时无法读取";
      $("#empty-description").textContent = "检查网络后，可以重新读取。";
      $("#empty-retry").hidden = false;
    }
    return false;
  } finally {
    if (generation === review.listGeneration) {
      review.listing = false;
      $("#photo-list").setAttribute("aria-busy", "false");
      updateControls();
      updatePhotoNavigation();
    }
  }
}

function draftFingerprint() {
  return JSON.stringify({ people: review.people, notes: $("#annotation-notes").value });
}

function markDirty() {
  review.notes = $("#annotation-notes").value;
  review.dirty = draftFingerprint() !== review.baseline;
  $("#dirty-dot").hidden = !review.dirty;
  $("#save-state").textContent = review.dirty ? "有修改尚未保存" :
    review.current?.annotation ? "已保存人工标注 · 修订 " + review.current.annotation.revision :
    "尚未人工确认 · 核对后请点击保存";
  updateControls();
}

function createSelect(options, value, label) {
  const select = element("select");
  select.dataset.personControl = "1";
  select.setAttribute("aria-label", label);
  for (const [key, text] of Object.entries(options)) {
    const option = element("option", "", text);
    option.value = key;
    select.append(option);
  }
  if (!Object.hasOwn(options, value)) {
    const option = element("option", "", "原有分类：" + String(value));
    option.value = String(value);
    select.append(option);
  }
  select.value = value;
  return select;
}

function renderPeople() {
  const fragment = document.createDocumentFragment();
  review.people.forEach((person, index) => {
    const prediction = (review.current?.predictions || []).find((item) => item.id === person.id);
    const card = element("section", "person-editor");
    card.dataset.personId = person.id;
    const heading = element("div", "person-heading");
    const title = element("strong");
    title.append(element("span", "person-number", String(index + 1)),
      document.createTextNode(prediction ? "识别人物" : "人工补充人物"));
    const remove = element("button", "", "移除误检");
    remove.type = "button";
    remove.dataset.personControl = "1";
    remove.setAttribute("aria-label", "移除人物 " + (index + 1));
    remove.addEventListener("click", () => {
      review.people = review.people.filter((item) => item.id !== person.id);
      renderPeople(); markDirty();
      notify("人物已从本次标注移除，保存后生效。");
    });
    heading.append(title, remove);
    card.append(heading);
    const info = element("div", "prediction-info");
    if (prediction) {
      info.append(element("span", "prediction-label", "机器：" + (prediction.identity || "未确认")));
      info.append(element("span", "", (prediction.posture_label || POSTURES[prediction.posture] || "无法判断") +
        " · " + (prediction.action_label || ACTIONS[prediction.action] || "无法判断")));
      const score = numberText(prediction.score);
      const similarity = numberText(prediction.similarity);
      if (score !== null) info.append(element("span", "", "姿态规则分数 " + score));
      if (similarity !== null) info.append(element("span", "", "人脸相似度 " + similarity));
      if (prediction.track_id !== null && prediction.track_id !== undefined) {
        info.append(element("span", "", "跟踪 #" + prediction.track_id));
      }
    } else {
      info.append(element("span", "", "人工补充 · 没有机器识别分数"));
    }
    card.append(info);
    const fields = element("div", "person-fields");
    const identityLabel = element("label", "", "正确人物（可自定义）");
    const identity = element("input");
    identity.type = "text";
    identity.value = person.identity;
    identity.maxLength = 64;
    identity.required = true;
    identity.autocomplete = "off";
    identity.setAttribute("list", "identity-suggestions");
    identity.dataset.personControl = "1";
    identity.dataset.identity = "1";
    identity.setAttribute("aria-label", "人物 " + (index + 1) + " 的正确身份");
    identity.addEventListener("input", () => {
      person.identity = identity.value;
      identity.classList.remove("field-invalid");
      markDirty();
    });
    identityLabel.append(identity);
    const postureLabel = element("label", "", "姿态");
    const posture = createSelect(POSTURES, person.posture, "人物 " + (index + 1) + " 的姿态");
    posture.dataset.posture = "1";
    posture.addEventListener("change", () => { person.posture = posture.value; markDirty(); });
    postureLabel.append(posture);
    const actionLabel = element("label", "", "动作");
    const action = createSelect(ACTIONS, person.action, "人物 " + (index + 1) + " 的动作");
    action.dataset.action = "1";
    action.addEventListener("change", () => { person.action = action.value; markDirty(); });
    actionLabel.append(action);
    fields.append(identityLabel, postureLabel, actionLabel);
    card.append(fields);
    let hint = "人物编号 " + person.id;
    if (prediction?.keypoints?.length) hint += " · " + prediction.keypoints.length + " 个关键点";
    if (Array.isArray(prediction?.bbox) && prediction.bbox.length === 4) {
      hint += " · 框 " + prediction.bbox.map((coordinate) => Math.round(coordinate)).join(", ");
    }
    if (!prediction) hint += " · 暂无人物框与关键点";
    card.append(element("p", "target-hint", hint));
    fragment.append(card);
  });
  $("#people-editor").replaceChildren(fragment);
  $("#no-people").hidden = review.people.length > 0;
  updateControls();
}

function setImageMode(mode) {
  if (!review.current) return;
  const original = mode === "original" && review.current.original_available;
  review.imageMode = original ? "original" : "annotated";
  const url = safeImageURL(original ? review.current.original_url : review.current.image_url);
  $("#image-error").hidden = !!url;
  $("#zoom-image").disabled = !url || review.expired;
  const image = $("#detail-image");
  image.alt = (original ? "无标注原片" : "机器识别标注图") + "，" + formatTime(review.current.sampled_at);
  if (url) image.src = url;
  else image.removeAttribute("src");
  $("#image-kind").textContent = original ? "无标注原片" : "机器识别 · 采集时标注";
  $("#show-original").setAttribute("aria-pressed", String(original));
  $("#show-annotated").setAttribute("aria-pressed", String(!original));
  $("#show-original").title = review.current.original_available ? "查看采集时未经标注的原始画面" :
    "此历史照片未保存无标注原片";
  $("#original-note").classList.toggle("historical", !review.current.original_available);
  $("#original-note").textContent = review.current.original_available ?
    original ? "这是未经标注的原始画面。切换标注图可对照机器识别结果。" :
      "图中保留采集时的机器标注，人工更正在下方独立保存。" :
    "此历史照片只保留了识别标注图，没有无标注原片。新采集的照片会同时保存原片。";
  updateControls();
}

function renderDetail() {
  const item = review.current;
  if (!item) return;
  const annotation = item.annotation;
  review.people = peopleOf(item).map((person) => ({
    id: person.id, identity: typeof person.identity === "string" ? person.identity : "未确认",
    posture: typeof person.posture === "string" ? person.posture : "unknown",
    action: typeof person.action === "string" ? person.action : "unknown",
  }));
  review.notes = typeof annotation?.notes === "string" ? annotation.notes : "";
  $("#annotation-notes").value = review.notes;
  review.baseline = draftFingerprint();
  review.dirty = false;
  review.conflict = false;
  $("#conflict-notice").hidden = true;
  showNotice("#editor-notice", "");
  $("#detail-heading").textContent = "照片审核";
  $("#detail-time").textContent = formatTime(item.sampled_at);
  const status = statusOf(item);
  $("#detail-status").textContent = STATUSES[status];
  $("#detail-status").className = "status-tag " + status;
  $("#detail-sequence").textContent = item.sequence === null || item.sequence === undefined ? "未记录" : String(item.sequence);
  $("#detail-model").textContent = typeof item.model === "string" && item.model ? item.model : "历史样本未记录";
  $("#detail-rules").textContent = typeof item.rules === "string" && item.rules ? item.rules : "历史样本未记录";
  renderPostures(item);
  $("#annotation-history").hidden = !annotation;
  if (annotation) $("#annotation-history").textContent = "人工标注修订 " + annotation.revision + " · " +
    formatTime(annotation.updated_at) + (status === "excluded" ? " · 已排除，确认保存可重新纳入训练资料" : "");
  $("#save-photo").textContent = annotation?.status === "confirmed" ? "更新并保存" : "确认并保存";
  $("#exclude-photo").textContent = status === "excluded" ? "更新排除记录" : "排除照片";
  $("#detail-empty").hidden = true;
  $("#photo-detail").hidden = false;
  renderPeople();
  setImageMode("annotated");
  markDirty();
  renderList();
}

function renderPostures(item) {
  const predictions = Array.isArray(item.predictions) ? item.predictions : [];
  const skeletons = predictions.filter((person) => Array.isArray(person.keypoints) && person.keypoints.length === 33);
  $("#machine-postures").textContent = skeletons.length ? skeletons.map((person) =>
    (person.identity || "未确认人物") + "：" + (POSTURES[person.posture] || "无法判断")).join("\n") :
    "未检测到完整人体，姿态无法判断";
  const annotation = item.annotation;
  $("#manual-postures").textContent = annotation?.status === "confirmed" && annotation.people.length ?
    annotation.people.map((person) => (person.identity || "未确认人物") + "：" +
      (POSTURES[person.posture] || "无法判断")).join("\n") : annotation?.status === "excluded" ?
    "已排除，暂无确认姿态" : "尚未人工确认，可在下方更正";
}

async function loadDetail(id, enterMobile = false) {
  const generation = ++review.detailGeneration;
  review.loading = true;
  $("#detail-panel").setAttribute("aria-busy", "true");
  $("#detail-loading").hidden = false;
  $("#photo-detail").hidden = true;
  $("#detail-empty").hidden = true;
  if (enterMobile) $("#review-layout").classList.add("detail-active");
  updateControls();
  try {
    const item = await json("/api/v1/photos/" + encodeURIComponent(id));
    if (generation !== review.detailGeneration) return false;
    if (!item || item.id !== id || !Array.isArray(item.predictions)) throw new ApiError("照片信息不完整，请重新读取。", 0);
    review.current = item;
    renderDetail();
    if (enterMobile && matchMedia("(max-width: 720px)").matches) {
      $("#detail-panel").scrollIntoView({ block: "start", behavior: "instant" });
    }
    return true;
  } catch (error) {
    if (generation !== review.detailGeneration) return false;
    if (!review.expired) showNotice("#page-notice", error.message, "error");
    $("#photo-detail").hidden = !review.current;
    $("#detail-empty").hidden = !!review.current;
    if (!review.current) $("#review-layout").classList.remove("detail-active");
    return false;
  } finally {
    if (generation === review.detailGeneration) {
      review.loading = false;
      $("#detail-loading").hidden = true;
      $("#detail-panel").setAttribute("aria-busy", "false");
      updateControls();
      updatePhotoNavigation();
    }
  }
}

function updateControls() {
  const busy = review.expired || review.loading || review.listing || review.saving || review.batching;
  for (const selector of ["#save-photo", "#save-next", "#exclude-photo"]) {
    $(selector).disabled = busy || !review.current || review.conflict;
  }
  $("#add-person").disabled = busy || !review.current || review.people.length >= 10;
  $("#add-person").title = review.people.length >= 10 ? "每张照片最多标注 10 位人物" : "补充机器遗漏的人物";
  for (const field of document.querySelectorAll("[data-person-control], #annotation-notes")) field.disabled = busy;
  for (const field of document.querySelectorAll("[data-filter], [data-category], #identity-filter, #posture-filter, #label-source, #refresh-list, #search-form button, .photo-open, .photo-select")) {
    field.disabled = busy || review.listing;
  }
  $("#photo-search").disabled = busy || review.listing;
  $("#page-prev").disabled = busy || review.listing || review.page <= 1;
  $("#page-next").disabled = busy || review.listing || review.page >= review.pages;
  $("#show-original").disabled = busy || !review.current?.original_available;
  $("#show-annotated").disabled = busy || !review.current;
  $("#zoom-image").disabled = busy || !review.current || !$("#detail-image").getAttribute("src") || !$("#image-error").hidden;
  $("#back-to-list").disabled = busy;
  $("#export-link").setAttribute("aria-disabled", String(review.expired || !review.csrf));
  $("#export-category").disabled = busy || !review.csrf;
  $("#export-selected").disabled = busy || !review.selected.size || !review.csrf;
  $("#delete-selected").disabled = busy || !review.selected.size || review.selectionConflict;
  $("#select-page").disabled = busy || !review.items.length;
  $("#clear-selection").disabled = busy || !review.selected.size;
  $("#sync-selection").disabled = busy || !review.selected.size;
  $("#confirm-delete").disabled = busy || !review.deleteCandidates.length;
  for (const radio of document.querySelectorAll('[name="delete-scope"]')) radio.disabled = busy;
  $("#annotation-form").setAttribute("aria-busy", String(review.saving));
  $("#save-next").textContent = review.saving ? "正在保存…" : "保存并看下一张 →";
}

function updatePhotoNavigation() {
  const index = review.items.findIndex((item) => item.id === review.current?.id);
  const busy = review.expired || review.loading || review.listing || review.saving || review.batching;
  $("#previous-photo").disabled = busy || !review.current || index < 0 || (index === 0 && review.page === 1);
  $("#next-photo").disabled = busy || !review.current ||
    (!review.items.length || (index === review.items.length - 1 && review.page >= review.pages));
  $("#detail-position").textContent = index >= 0 ?
    ((review.page - 1) * 24 + index + 1) + " / " + review.total : review.current ? "已保存 · 当前筛选外" : "—";
}

function guarded(callback) {
  if (review.saving || review.loading || review.listing || review.batching) return;
  if (review.dirty) {
    review.pendingAction = callback;
    if (!$("#unsaved-dialog").open) $("#unsaved-dialog").showModal();
  } else callback();
}

async function navigatePhoto(delta) {
  const index = review.items.findIndex((item) => item.id === review.current?.id);
  if (index < 0) {
    const item = delta > 0 ? review.items[0] : review.items[review.items.length - 1];
    if (item) await loadDetail(item.id, true);
    return;
  }
  const next = review.items[index + delta];
  if (next) { await loadDetail(next.id, true); return; }
  const page = review.page + delta;
  if (page < 1 || page > review.pages) return;
  if (await loadList(page)) {
    const candidate = delta > 0 ? review.items[0] : review.items[review.items.length - 1];
    if (candidate) await loadDetail(candidate.id, true);
  }
}

function validatePeople(status) {
  if (status === "confirmed" && !review.people.length) {
    showNotice("#editor-notice", "请至少填写一位人物。没有人的照片请选择排除。", "error");
    $("#add-person").focus();
    return false;
  }
  if (review.people.length > 10) {
    showNotice("#editor-notice", "每张照片最多标注 10 位人物，请先移除误检。", "error");
    return false;
  }
  const invalid = review.people.findIndex((person) => !person.identity.trim() || person.identity.length > 64 ||
    /[\u0000-\u001f]/.test(person.identity) || !Object.hasOwn(POSTURES, person.posture) || !Object.hasOwn(ACTIONS, person.action));
  if (invalid >= 0) {
    const card = $("#people-editor").children[invalid];
    const input = card.querySelector("[data-identity]");
    input.classList.add("field-invalid"); input.focus();
    showNotice("#editor-notice", "请填写人物 " + (invalid + 1) + " 的正确名称，并选择有效的姿态和动作。", "error");
    return false;
  }
  return true;
}

async function saveAnnotation(status, thenNext = false) {
  if (review.saving || review.loading || review.listing || review.batching || !review.current || review.expired || review.conflict) return;
  if (status === "confirmed" && !validatePeople(status)) return;
  const previousId = review.current.id;
  const previousIndex = review.items.findIndex((item) => item.id === previousId);
  const successorId = review.items[previousIndex + 1]?.id;
  const body = {
    expected_revision: revisionOf(review.current), status,
    people: status === "excluded" ? [] : review.people.map((person) => ({ ...person, identity: person.identity.trim() })),
    notes: $("#annotation-notes").value,
  };
  review.saving = true;
  updateControls(); updatePhotoNavigation();
  showNotice("#editor-notice", "");
  let saved = false;
  try {
    const item = await json("/api/v1/photos/" + encodeURIComponent(previousId) + "/annotation", {
      method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": review.csrf },
      body: JSON.stringify(body),
    });
    if (!item || item.id !== previousId || !item.annotation) {
      throw new ApiError("保存结果尚未确认，请重新读取这张照片后核对。", 0);
    }
    review.current = item;
    if (review.selected.has(item.id)) review.selected.set(item.id, selectionEntry(item));
    renderDetail();
    saved = true;
    notify(status === "excluded" ? "已排除，不会纳入训练资料。" : "人工标注已单独保存。" );
    showNotice("#editor-notice", status === "excluded" ? "已排除。以后仍可更正人物并确认保存。" :
      "已保存，原始识别结果保留供对照。", "good");
    const refreshed = await loadList();
    if (thenNext && refreshed) {
      let candidateId = successorId;
      if (!candidateId && previousIndex >= 0 && !review.items.some((photo) => photo.id === previousId)) {
        candidateId = review.items[previousIndex]?.id;
      }
      if (candidateId && candidateId !== previousId) await loadDetail(candidateId, true);
      else if (review.page < review.pages && await loadList(review.page + 1)) {
        if (review.items[0]) await loadDetail(review.items[0].id, true);
      } else notify("已保存，这是当前列表的最后一张照片。" );
    }
  } catch (error) {
    if (error.status === 409) {
      review.conflict = true;
      $("#conflict-notice").hidden = false;
      $("#conflict-notice").scrollIntoView({ block: "nearest" });
    } else if (!review.expired) {
      showNotice("#editor-notice", saved ? "标注已保存，但列表刷新失败。请稍后刷新列表。" : error.message, "error");
    }
  } finally {
    review.saving = false;
    updateControls(); updatePhotoNavigation();
  }
}

function mutationOptions(body) {
  return { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": review.csrf },
    body: JSON.stringify(body) };
}

async function download(response, filename) {
  const url = URL.createObjectURL(await response.blob());
  const link = element("a");
  link.href = url; link.download = filename;
  document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

async function exportPhotos(selected = false) {
  if (review.expired || review.batching || !review.csrf) return;
  if (selected) {
    if (!review.selected.size) { notify("请先选择需要导出的照片。" ); return; }
    const unfinished = [...review.selected.values()].filter((item) => item.status !== "confirmed").length;
    if (unfinished) {
      showNotice("#page-notice", "已选照片中有 " + unfinished + " 张尚未人工确认或已排除，不能作为训练资料导出。请清除选择，并在“已确认”中重新选择。", "error");
      return;
    }
  }
  if (review.dirty) notify("导出使用已保存的人工标注，当前未保存修改不会包含在照片包里。" );
  const body = { ids: selected ? [...review.selected.keys()] : [], identity: selected ? "" : review.identity,
    posture: selected ? "" : review.posture, label_source: "manual" };
  review.batching = true;
  updateControls(); updatePhotoNavigation();
  const button = selected ? $("#export-selected") : $("#export-category");
  const label = button.textContent; button.textContent = "正在打包…";
  try {
    const response = await request("/api/v1/photo-labels/export.zip", mutationOptions(body));
    const name = (body.identity || (selected ? "选中照片" : "全部人物")).replace(/[\\/:*?"<>|]/g, "_");
    await download(response, "人物训练照片-" + name + (body.posture ? "-" + POSTURES[body.posture] : "") + ".zip");
    showNotice("#page-notice", "照片包已导出，包含照片、人工标注、机器识别结果和训练清单。仅含人工已确认的记录。", "good");
  } catch (error) {
    if (!review.expired) showNotice("#page-notice", error.message + (error.status === 413 ? " 每次最多 200 张，请勾选后分批导出。" : ""), "error");
  } finally {
    review.batching = false; button.textContent = label;
    updateControls(); updatePhotoNavigation();
  }
}

function openDeleteDialog() {
  if (!review.selected.size || review.selectionConflict) return;
  review.deleteScope = "labels";
  for (const radio of document.querySelectorAll('[name="delete-scope"]')) radio.checked = radio.value === "labels";
  updateDeleteDialog();
  $("#delete-dialog").showModal();
}

function updateDeleteDialog() {
  const photos = review.deleteScope === "photos";
  const entries = [...review.selected.entries()].filter(([, item]) => photos || item.hasAnnotation);
  review.deleteCandidates = entries.map(([id, item]) => ({ id, expected_revision: item.revision }));
  const others = review.selected.size - entries.length;
  $("#delete-title").textContent = photos ? "删除 " + entries.length + " 张照片及全部标注？" :
    entries.length ? "删除 " + entries.length + " 份人工标注？" : "所选照片没有人工标注";
  $("#delete-description").textContent = photos ?
    "所选照片、无标注原片、机器识别和人工标注将从列表及训练资料中移除。请核对删除范围。" :
    "照片和机器识别结果会保留。删除的人工标注不再用于训练，照片回到待审核状态。";
  $("#delete-count").textContent = photos ? "本次删除 " + entries.length + " 张照片，以及它们的原片和全部标注。" :
    entries.length ? "只删除人工标注，保留照片和机器识别结果。" +
      (others ? " 另有 " + others + " 张待审核照片不受影响。" : "") :
      "待审核照片没有人工标注。如需从列表移除照片，请明确选择“照片、原片和全部标注”。";
  $("#confirm-delete").textContent = photos ? "确认删除照片及全部标注" : "确认删除人工标注";
  updateControls();
}

function clearCurrent() {
  review.current = null; review.people = []; review.notes = ""; review.baseline = "";
  review.dirty = false; review.conflict = false;
  $("#annotation-notes").value = "";
  $("#photo-detail").hidden = true; $("#detail-empty").hidden = false;
  $("#detail-image").removeAttribute("src");
  $("#review-layout").classList.remove("detail-active");
  updateControls(); updatePhotoNavigation();
}

async function deleteLabels() {
  if (review.expired || review.batching || !review.deleteCandidates.length || review.selectionConflict) return;
  const candidates = review.deleteCandidates.map((item) => ({ ...item }));
  const scope = review.deleteScope;
  $("#delete-dialog").close();
  review.batching = true;
  updateControls(); updatePhotoNavigation();
  try {
    const result = await json("/api/v1/photo-labels/delete", mutationOptions({ items: candidates, scope }));
    const ids = Array.isArray(result.ids) ? result.ids : candidates.map((item) => item.id);
    for (const id of ids) review.selected.delete(id);
    review.selectionConflict = false;
    review.deleteCandidates = [];
    if (scope === "photos" && review.current && ids.includes(review.current.id)) clearCurrent();
    renderSelection();
    await loadList();
    if (review.current && ids.includes(review.current.id)) await loadDetail(review.current.id);
    const count = Number.isSafeInteger(result.deleted) ? result.deleted : ids.length;
    showNotice("#page-notice", scope === "photos" ? "已删除 " + count + " 张照片及全部标注，已从采集列表和训练资料移除。" :
      "已删除 " + count + " 份人工标注。照片和机器识别结果保留，已从训练资料中移除。", "good");
  } catch (error) {
    if (error.status === 409) {
      review.selectionConflict = true;
      showNotice("#page-notice", "部分标注已在其他页面修改，本次没有删除。选择仍保留，请重新同步并核对后操作。", "error");
      renderSelection();
    } else if (!review.expired) showNotice("#page-notice", error.message, "error");
  } finally {
    review.batching = false;
    updateControls(); updatePhotoNavigation();
  }
}

async function syncSelection() {
  if (review.expired || review.batching || !review.selected.size) return;
  review.batching = true;
  updateControls(); updatePhotoNavigation();
  try {
    const ids = [...review.selected.keys()];
    const results = [];
    for (let offset = 0; offset < ids.length; offset += 8) {
      const group = await Promise.all(ids.slice(offset, offset + 8).map(async (id) => {
        try { return await json("/api/v1/photos/" + encodeURIComponent(id)); }
        catch (error) { if (error.status === 404) return { id, missing: true }; throw error; }
      }));
      results.push(...group);
    }
    let missing = 0;
    for (const item of results) {
      if (item.missing) { review.selected.delete(item.id); missing += 1; }
      else review.selected.set(item.id, selectionEntry(item));
    }
    review.selectionConflict = false;
    await loadList();
    const current = results.find((item) => item.id === review.current?.id);
    if (current?.missing) clearCurrent();
    else if (current) { review.current = current; renderDetail(); }
    renderSelection();
    showNotice("#page-notice", "已同步所选照片的最新人工标注。请核对后再删除。" +
      (missing ? " " + missing + " 张已不存在，已从选择中移除。" : ""), "good");
  } catch (error) {
    if (!review.expired) showNotice("#page-notice", error.message, "error");
  } finally {
    review.batching = false;
    updateControls(); updatePhotoNavigation();
  }
}

$("#annotation-form").addEventListener("submit", (event) => {
  event.preventDefault(); saveAnnotation("confirmed");
});
$("#save-next").addEventListener("click", () => saveAnnotation("confirmed", true));
$("#annotation-notes").addEventListener("input", markDirty);
$("#add-person").addEventListener("click", () => {
  if (review.people.length >= 10 || !review.current) return;
  const random = typeof crypto.randomUUID === "function" ? crypto.randomUUID().replaceAll("-", "").slice(0, 20) :
    Date.now().toString(36) + Math.random().toString(36).slice(2, 12);
  review.people.push({ id: "manual-" + random, identity: "未确认", posture: "unknown", action: "unknown" });
  renderPeople(); markDirty();
  $("#people-editor").lastElementChild.querySelector("input").focus();
});
$("#exclude-photo").addEventListener("click", () => $("#exclude-dialog").showModal());
$("#cancel-exclude").addEventListener("click", () => $("#exclude-dialog").close());
$("#confirm-exclude").addEventListener("click", () => { $("#exclude-dialog").close(); saveAnnotation("excluded"); });
$("#show-annotated").addEventListener("click", () => setImageMode("annotated"));
$("#show-original").addEventListener("click", () => setImageMode("original"));
$("#detail-image").addEventListener("error", async () => {
  $("#image-error").hidden = false;
  $("#zoom-image").disabled = true;
  try { await json("/api/v1/auth/session"); } catch (_) { /* request handles expiration. */ }
});
$("#detail-image").addEventListener("load", () => { $("#image-error").hidden = true; updateControls(); });
$("#zoom-image").addEventListener("click", () => {
  const src = $("#detail-image").getAttribute("src");
  if (!src) return;
  $("#zoomed-image").src = src;
  $("#zoomed-image").alt = $("#detail-image").alt;
  $("#zoom-title").textContent = formatTime(review.current?.sampled_at) + " · " +
    (review.imageMode === "original" ? "无标注原片" : "识别标注图");
  $("#zoom-dialog").showModal();
});
$("#zoom-close").addEventListener("click", () => $("#zoom-dialog").close());
$("#previous-photo").addEventListener("click", () => guarded(() => navigatePhoto(-1)));
$("#next-photo").addEventListener("click", () => guarded(() => navigatePhoto(1)));
$("#back-to-list").addEventListener("click", () => guarded(() => {
  $("#review-layout").classList.remove("detail-active"); $("#library-heading").scrollIntoView({ block: "start" });
}));
$("#reload-detail").addEventListener("click", () => guarded(() => loadDetail(review.current.id)));
$("#refresh-list").addEventListener("click", () => guarded(async () => {
  await loadList(); if (review.current) await loadDetail(review.current.id);
}));
$("#empty-retry").addEventListener("click", () => guarded(() => loadList()));
$("#page-prev").addEventListener("click", () => guarded(() => loadList(review.page - 1)));
$("#page-next").addEventListener("click", () => guarded(() => loadList(review.page + 1)));
$("#identity-filter").addEventListener("change", () => selectIdentity($("#identity-filter").value));
$("#posture-filter").addEventListener("change", () => {
  const posture = $("#posture-filter").value;
  guarded(() => { review.posture = posture; renderFilterControls(); loadList(1); });
  renderFilterControls();
});
$("#label-source").addEventListener("change", () => {
  const source = $("#label-source").value;
  guarded(() => {
    review.labelSource = source;
    review.filter = source === "manual" ? "confirmed" : "all";
    renderFilterControls(); loadList(1);
  });
  renderFilterControls();
});
$("#select-page").addEventListener("change", () => {
  const selected = $("#select-page").checked;
  let limit = false;
  for (const item of review.items) {
    if (selected && !review.selected.has(item.id)) {
      if (review.selected.size >= 200) { limit = true; continue; }
      review.selected.set(item.id, selectionEntry(item));
    } else if (!selected) review.selected.delete(item.id);
  }
  if (!review.selected.size) review.selectionConflict = false;
  renderSelection();
  if (limit) notify("已达到 200 张选择上限，其余照片未选中。请分批处理。" );
});
$("#clear-selection").addEventListener("click", () => {
  review.selected.clear(); review.selectionConflict = false; renderSelection();
});
$("#export-selected").addEventListener("click", () => exportPhotos(true));
$("#export-category").addEventListener("click", () => exportPhotos(false));
$("#delete-selected").addEventListener("click", () => guarded(openDeleteDialog));
$("#cancel-delete").addEventListener("click", () => {
  $("#delete-dialog").close(); review.deleteCandidates = [];
});
$("#confirm-delete").addEventListener("click", deleteLabels);
for (const radio of document.querySelectorAll('[name="delete-scope"]')) {
  radio.addEventListener("change", () => { review.deleteScope = radio.value; updateDeleteDialog(); });
}
$("#sync-selection").addEventListener("click", () => guarded(syncSelection));
for (const button of document.querySelectorAll("[data-filter]")) {
  button.addEventListener("click", () => {
    if (button.dataset.filter === review.filter) return;
    guarded(() => {
      review.filter = button.dataset.filter;
      if (review.filter === "pending") review.labelSource = "effective";
      renderFilterControls(); loadList(1);
    });
  });
}
$("#search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const query = $("#photo-search").value.trim();
  guarded(() => { review.query = query; loadList(1); });
});
$("#photo-search").addEventListener("search", () => {
  if (!$("#photo-search").value && review.query) guarded(() => { review.query = ""; loadList(1); });
});
$("#stay-editing").addEventListener("click", () => {
  review.pendingAction = null; $("#unsaved-dialog").close(); renderFilterControls();
});
$("#unsaved-dialog").addEventListener("cancel", () => { review.pendingAction = null; });
$("#discard-changes").addEventListener("click", () => {
  const callback = review.pendingAction;
  review.pendingAction = null;
  $("#unsaved-dialog").close();
  if (review.current) renderDetail();
  if (callback) callback();
});
for (const link of document.querySelectorAll('a[href="/"], a[href="/photos"]')) {
  link.addEventListener("click", (event) => {
    if (!review.dirty || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault(); guarded(() => { location.href = link.href; });
  });
}
$("#export-link").addEventListener("click", async (event) => {
  event.preventDefault();
  if (review.expired || !review.csrf || $("#export-link").dataset.busy) return;
  $("#export-link").dataset.busy = "1";
  $("#export-link").textContent = "正在导出…";
  try {
    const response = await request("/api/v1/photo-labels/export");
    await download(response, "人物照片人工标注.jsonl");
    notify("已导出人工确认的标注清单。" );
  } catch (error) {
    if (!review.expired) showNotice("#page-notice", error.message, "error");
  } finally {
    $("#export-link").dataset.busy = "";
    $("#export-link").textContent = "导出已确认标注";
  }
});
window.addEventListener("beforeunload", (event) => {
  if (review.dirty || review.saving || review.batching) { event.preventDefault(); event.returnValue = ""; }
});
document.addEventListener("keydown", (event) => {
  if (event.altKey && !event.ctrlKey && !event.metaKey && ["ArrowLeft", "ArrowRight"].includes(event.key) &&
      !document.querySelector("dialog[open]") && review.current) {
    event.preventDefault(); guarded(() => navigatePhoto(event.key === "ArrowLeft" ? -1 : 1));
  }
});

(async () => {
  updateControls();
  try {
    const session = await json("/api/v1/auth/session");
    if (typeof session.csrf !== "string" || !session.csrf) throw new ApiError("登录状态无法确认，请重新登录。", 0);
    review.csrf = session.csrf;
    await loadList(1);
  } catch (error) {
    $("#photo-list").setAttribute("aria-busy", "false");
    if (!review.expired) {
      showNotice("#page-notice", error.message, "error");
      $("#list-empty").hidden = false;
      $("#empty-title").textContent = "照片列表暂时无法读取";
      $("#empty-description").textContent = "重新打开本页，或检查老大 Mini 的连接。";
    }
  } finally { updateControls(); }
})();
