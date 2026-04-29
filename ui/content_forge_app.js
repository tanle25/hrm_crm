(function () {
    const API_BASE = "/api";
    const PAGE_STEPS = [
        "deduplicator",
        "fetcher",
        "extractor",
        "knowledge",
        "enricher",
        "planner",
        "image_selector",
        "media_uploader",
        "writer",
        "humanizer",
        "internal_linker",
        "qa",
        "seo_adjuster",
        "publisher",
    ];
    const state = {
        selectedJobId: localStorage.getItem("content_forge_selected_job_id") || "",
        selectedSiteId: "",
        selectedSubmitSiteIds: JSON.parse(localStorage.getItem("content_forge_submit_site_ids") || "[]"),
        selectedFacebookCreatePageIds: JSON.parse(localStorage.getItem("content_forge_fb_create_page_ids") || "[]"),
        selectedFacebookCreateGroups: JSON.parse(localStorage.getItem("content_forge_fb_create_groups") || "[]"),
        facebookCreateImages: [],
        facebookCreatePreview: null,
        selectedShopeeSiteIds: JSON.parse(localStorage.getItem("content_forge_shopee_site_ids") || "[]"),
        selectedShopeeItemId: localStorage.getItem("content_forge_shopee_item_id") || "",
        jobsSocket: null,
        jobsSocketReconnectTimer: null,
        jobsPollTimer: null,
        jobsReconnectAttempts: 0,
        jobsStreamActive: false,
        jobsSignature: "",
        detailSocket: null,
        detailSocketReconnectTimer: null,
        detailReconnectAttempts: 0,
        detailStreamActive: false,
        detailSignature: "",
        jobsStatusFilter: "",
        jobsSortKey: "",
        jobsSortDir: "asc",
        selectedFacebookCommentId: "",
        facebookPostsSyncing: false,
        facebookCommentsSyncing: false,
        facebookPostsAutoSynced: false,
        facebookCommentsAutoSynced: false,
        facebookPostsOffset: 0,
        facebookPostsLimit: 20,
        facebookStatsSyncing: false,
        facebookStatsAutoSynced: false,
        facebookPageGroupFilter: "",
        facebookPageGroups: [],
        selectedFacebookConversationId: "",
        facebookConversations: [],
        facebookMessagesSocket: null,
        facebookMessagesSocketReconnectTimer: null,
        facebookMessagesReconnectAttempts: 0,
        facebookMessagesStreamActive: false,
        facebookMessagesFallbackTimer: null,
        facebookMessagesFallbackActive: false,
        facebookMessagesSyncJobId: "",
        facebookMessagesSyncing: false,
        facebookMessagesAutoSynced: false,
        facebookConversationDetails: {},
        facebookConversationDetailPending: {},
        facebookMessageDraftMedia: [],
        facebookSlashCommands: null,
        facebookSlashCommandsLoaded: false,
        facebookSlashEditingCommand: "",
        facebookCreateScheduleMode: "now",
        facebookCreateScheduledAt: "",
        facebookContentJobsRefreshTimer: null,
    };

    function escapeHtml(value) {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function truncate(value, length = 80) {
        const text = String(value ?? "");
        return text.length > length ? `${text.slice(0, length - 1)}…` : text;
    }

    function formatNumber(value, digits = 0) {
        const numeric = Number(value ?? 0);
        return Number.isFinite(numeric)
            ? numeric.toLocaleString("vi-VN", { maximumFractionDigits: digits, minimumFractionDigits: digits })
            : "0";
    }

    function formatMoney(value) {
        return `$${formatNumber(Number(value ?? 0), 3)}`;
    }

    function formatCompact(value) {
        const numeric = Number(value ?? 0);
        if (!Number.isFinite(numeric)) return "0";
        return new Intl.NumberFormat("vi-VN", { notation: "compact", maximumFractionDigits: 1 }).format(numeric);
    }

    function maskSecret(value) {
        const text = String(value ?? "");
        if (!text) return "-";
        if (text.length <= 6) return "••••••";
        return `${text.slice(0, 2)}••••${text.slice(-2)}`;
    }

    function redactSensitiveText(value) {
        return String(value ?? "").replace(/access_token=[^&\s"'<>]+/gi, "access_token=***");
    }

    async function copyTextToClipboard(value, inputEl = null) {
        const text = String(value || "");
        if (!text) return false;
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(text);
            return true;
        }
        const target = inputEl || document.createElement("textarea");
        const temporary = !inputEl;
        if (temporary) {
            target.value = text;
            target.setAttribute("readonly", "readonly");
            target.style.position = "fixed";
            target.style.top = "-1000px";
            document.body.appendChild(target);
        }
        target.focus();
        target.select();
        target.setSelectionRange?.(0, text.length);
        const copied = document.execCommand("copy");
        if (temporary) target.remove();
        return copied;
    }

    function formatDate(value) {
        if (!value) return "-";
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return String(value);
        return date.toLocaleString("vi-VN");
    }

    function badge(status) {
        const key = String(status || "").toLowerCase();
        if (["completed", "published"].includes(key)) return ["green", "PUBLISHED"];
        if (["processing", "queued", "pending"].includes(key)) return ["amber", key.toUpperCase()];
        if (["failed", "dlq"].includes(key)) return ["red", key.toUpperCase()];
        if (key === "duplicate") return ["purple", "DUPLICATE"];
        return ["cyan", String(status || "-").toUpperCase()];
    }

    function stepLabel(step) {
        const map = {
            deduplicator: "DEDUP",
            fetcher: "FETCHER",
            extractor: "EXTRACT",
            knowledge: "KNOWLEDGE",
            enricher: "ENRICH",
            planner: "PLANNER",
            image_selector: "IMG SELECT",
            media_uploader: "UPLOAD",
            writer: "WRITER",
            humanizer: "HUMANIZE",
            internal_linker: "LINKER",
            qa: "QA",
            seo_adjuster: "SEO",
            publisher: "PUBLISH",
        };
        return map[step] || String(step || "").toUpperCase();
    }

    async function fetchJSON(path, options) {
        const headers = options?.body instanceof FormData
            ? { ...(options?.headers || {}) }
            : { "Content-Type": "application/json", ...(options?.headers || {}) };
        const response = await fetch(`${API_BASE}${path}`, {
            ...options,
            headers,
        });
        if (!response.ok) {
            let message = `${response.status} ${response.statusText}`;
            try {
                const errorBody = await response.json();
                message = errorBody.detail || errorBody.message || message;
            } catch (_) {}
            if (response.status === 401) {
                window.location.replace("/login");
                throw new Error("Authentication required");
            }
            throw new Error(message);
        }
        return response.json();
    }

    function setSelectedJob(jobId) {
        state.selectedJobId = jobId || "";
        localStorage.setItem("content_forge_selected_job_id", state.selectedJobId);
    }

    function setSelectedSubmitSites(siteIds) {
        state.selectedSubmitSiteIds = Array.isArray(siteIds) ? siteIds.filter(Boolean) : [];
        localStorage.setItem("content_forge_submit_site_ids", JSON.stringify(state.selectedSubmitSiteIds));
    }

    function setSelectedFacebookCreatePages(pageIds) {
        state.selectedFacebookCreatePageIds = Array.isArray(pageIds) ? pageIds.filter(Boolean) : [];
        localStorage.setItem("content_forge_fb_create_page_ids", JSON.stringify(state.selectedFacebookCreatePageIds));
    }

    function setSelectedFacebookCreateGroups(groups) {
        state.selectedFacebookCreateGroups = Array.isArray(groups) ? groups.filter(Boolean) : [];
        localStorage.setItem("content_forge_fb_create_groups", JSON.stringify(state.selectedFacebookCreateGroups));
    }

    function setSelectedShopeeSites(siteIds) {
        state.selectedShopeeSiteIds = Array.isArray(siteIds) ? siteIds.filter(Boolean) : [];
        localStorage.setItem("content_forge_shopee_site_ids", JSON.stringify(state.selectedShopeeSiteIds));
    }

    function setSelectedShopeeItem(itemId) {
        state.selectedShopeeItemId = itemId || "";
        localStorage.setItem("content_forge_shopee_item_id", state.selectedShopeeItemId);
    }

    function showFeedback(type, message) {
        const box = document.getElementById("submit-feedback");
        if (!box) return;
        box.className = `mb-4 text-[11px] border p-3 ${type === "error" ? "text-hud-red border-hud-red/30 bg-hud-red/10" : "text-hud-green border-hud-green/30 bg-hud-green/10"}`;
        box.textContent = message;
        box.classList.remove("hidden");
    }

    async function renderRecentSubmissions() {
        const container = document.getElementById("recent-submissions");
        if (!container) return;
        try {
            const payload = await fetchJSON("/jobs?limit=5");
            if (!payload.jobs.length) {
                container.innerHTML = `<div class="text-hud-muted text-[11px]">No jobs yet.</div>`;
                return;
            }
            container.innerHTML = payload.jobs.slice(0, 5).map((job) => {
                const [tone, label] = badge(job.status);
                return `
                    <div class="flex items-center gap-3 py-2 border-b border-hud-cyan/10">
                        <span class="status-dot ${tone}"></span>
                        <button class="text-left text-white font-bold truncate flex-1 job-open-link" data-job-id="${escapeHtml(job.job_id)}">
                            ${escapeHtml(truncate(job.url || job.title || job.job_id, 72))}
                        </button>
                        <span class="badge ${tone}">${escapeHtml(label)}</span>
                        <span class="text-hud-muted text-[10px]">${escapeHtml(job.updated_at ? formatDate(job.updated_at) : "-")}</span>
                    </div>
                `;
            }).join("");
            bindJobOpenLinks(container);
        } catch (error) {
            container.innerHTML = `<div class="text-hud-red text-[11px]">Failed to load recent jobs: ${escapeHtml(error.message)}</div>`;
        }
    }

    async function renderSiteMultiSelect(options) {
        const {
            listId,
            helpId,
            summaryId,
            checkboxClass,
            selectedIds,
            setSelectedIds,
            emptyHelpText,
            loadedHelpText,
        } = options;
        const container = document.getElementById(listId);
        const help = document.getElementById(helpId);
        const summary = document.getElementById(summaryId);
        if (!container) return;
        try {
            const payload = await fetchJSON("/sites");
            const sites = payload.sites || [];
            const validSelected = (selectedIds || []).filter((siteId) => sites.some((site) => site.site_id === siteId));
            setSelectedIds(validSelected);
            container.innerHTML = sites.length ? sites.map((site) => `
                <label class="flex items-center gap-3 border border-hud-cyan/12 bg-black/25 px-3 py-2 hover:border-hud-cyan/30 transition cursor-pointer">
                    <input type="checkbox" class="${escapeHtml(checkboxClass)}" value="${escapeHtml(site.site_id)}" ${validSelected.includes(site.site_id) ? "checked" : ""}/>
                    <div class="flex-1 min-w-0 flex items-center gap-3 text-[11px]">
                        <div class="text-white font-bold truncate max-w-[180px]">${escapeHtml(site.site_name || site.url)}</div>
                        <div class="text-hud-cyan truncate flex-1">${escapeHtml(site.url)}</div>
                        <div class="text-hud-muted truncate max-w-[120px] text-right">${escapeHtml(site.topic || "-")}</div>
                    </div>
                </label>
            `).join("") : `<div class="text-[11px] text-hud-muted">Chưa có site nào.</div>`;
            if (help) {
                help.textContent = sites.length
                    ? loadedHelpText
                    : emptyHelpText;
                help.className = `text-[10px] mt-2 ${sites.length ? "text-hud-muted" : "text-hud-amber"}`;
            }
            updateSiteSummary(summary, validSelected, sites);
            container.querySelectorAll(`.${checkboxClass}`).forEach((checkbox) => checkbox.addEventListener("change", () => {
                const selected = Array.from(container.querySelectorAll(`.${checkboxClass}:checked`)).map((input) => input.value);
                setSelectedIds(selected);
                updateSiteSummary(summary, selected, sites);
            }));
        } catch (error) {
            container.innerHTML = `<div class="text-[11px] text-hud-red">Không tải được danh sách site.</div>`;
            if (summary) summary.textContent = "Không tải được danh sách site";
            if (help) {
                help.textContent = `Không tải được site: ${error.message}`;
                help.className = "text-[10px] mt-2 text-hud-red";
            }
        }
    }

    async function renderSubmitSiteOptions() {
        await renderSiteMultiSelect({
            listId: "submit-site-list",
            helpId: "submit-site-help",
            summaryId: "submit-site-summary",
            checkboxClass: "submit-site-checkbox",
            selectedIds: state.selectedSubmitSiteIds,
            setSelectedIds: setSelectedSubmitSites,
            emptyHelpText: "Chưa có site nào. Tạo site ở màn Quản Lý Website trước.",
            loadedHelpText: "Có thể chọn nhiều site cho cùng một batch URL.",
        });
    }

    function renderFacebookCreateSummary() {
        const help = document.getElementById("fb-create-help");
        if (!help) return;
        help.textContent = `${state.selectedFacebookCreateGroups.length} nhóm · ${state.selectedFacebookCreatePageIds.length} page · ${state.facebookCreateImages.length} ảnh đã chọn`;
        help.className = "text-[10px] text-hud-muted mt-3";
    }

    async function renderFacebookCreateTargets() {
        const pageList = document.getElementById("fb-create-page-list");
        const groupList = document.getElementById("fb-create-group-list");
        if (!pageList || !groupList) return;
        try {
            const [pagesPayload, groupsPayload] = await Promise.all([
                fetchJSON("/facebook/pages"),
                fetchJSON("/facebook/page-groups").catch(() => ({ groups: [] })),
            ]);
            const pages = pagesPayload.pages || [];
            const groups = groupsPayload.groups || [];
            const validPageIds = state.selectedFacebookCreatePageIds.filter((pageId) => pages.some((page) => String(page.page_id || "") === pageId));
            const validGroups = state.selectedFacebookCreateGroups.filter((group) => groups.some((item) => String(item.name || "") === group));
            setSelectedFacebookCreatePages(validPageIds);
            setSelectedFacebookCreateGroups(validGroups);
            groupList.innerHTML = groups.length ? groups.map((group) => {
                const name = String(group.name || "");
                return `
                    <button type="button" class="fb-create-group-chip badge ${validGroups.includes(name) ? "green" : "cyan"} hover:brightness-125" data-group="${escapeHtml(name)}">
                        ${escapeHtml(name)} · ${formatNumber(group.page_count || 0)}
                    </button>
                `;
            }).join("") : `<div class="text-[11px] text-hud-muted px-2 py-2">Chưa có nhóm page.</div>`;
            pageList.innerHTML = pages.length ? pages.map((page) => {
                const pageId = String(page.page_id || "");
                return `
                    <label class="fb-create-page-row flex items-center gap-3 rounded-xl border border-hud-fb/12 bg-black/25 px-3 py-2 hover:border-hud-fb/40 transition cursor-pointer" data-page-id="${escapeHtml(pageId)}" data-page-group="${escapeHtml(page.group || "")}">
                        <input type="checkbox" class="fb-create-page-checkbox" value="${escapeHtml(pageId)}" ${validPageIds.includes(pageId) ? "checked" : ""}/>
                        <div class="w-9 h-9 rounded-full overflow-hidden bg-hud-fb/10 flex items-center justify-center flex-shrink-0">
                            ${page.picture_url ? `<img src="${escapeHtml(page.picture_url)}" alt="" class="w-full h-full object-cover"/>` : `<i class="fa-brands fa-facebook text-hud-fb text-xs"></i>`}
                        </div>
                        <div class="flex-1 min-w-0">
                            <div class="text-white text-[12px] font-bold truncate">${escapeHtml(page.name || pageId)}</div>
                            <div class="text-[10px] text-hud-muted truncate">${escapeHtml(page.group || "Chưa có nhóm")}</div>
                        </div>
                    </label>
                `;
            }).join("") : `<div class="text-[11px] text-hud-muted px-2 py-2">Chưa có fanpage nào.</div>`;
            groupList.querySelectorAll(".fb-create-group-chip").forEach((button) => {
                button.addEventListener("click", () => {
                    const group = button.dataset.group || "";
                    const nextGroups = state.selectedFacebookCreateGroups.includes(group)
                        ? state.selectedFacebookCreateGroups.filter((item) => item !== group)
                        : [...state.selectedFacebookCreateGroups, group];
                    setSelectedFacebookCreateGroups(nextGroups);
                    button.classList.toggle("green", nextGroups.includes(group));
                    button.classList.toggle("cyan", !nextGroups.includes(group));
                    pageList.querySelectorAll(".fb-create-page-row").forEach((row) => {
                        if (row.dataset.pageGroup !== group) return;
                        const checkbox = row.querySelector(".fb-create-page-checkbox");
                        if (checkbox) checkbox.checked = nextGroups.includes(group);
                    });
                    setSelectedFacebookCreatePages(Array.from(pageList.querySelectorAll(".fb-create-page-checkbox:checked")).map((input) => input.value));
                    renderFacebookCreateSummary();
                });
            });
            pageList.querySelectorAll(".fb-create-page-checkbox").forEach((checkbox) => {
                checkbox.addEventListener("change", () => {
                    setSelectedFacebookCreatePages(Array.from(pageList.querySelectorAll(".fb-create-page-checkbox:checked")).map((input) => input.value));
                    renderFacebookCreateSummary();
                });
            });
            renderFacebookCreateSummary();
        } catch (error) {
            groupList.innerHTML = `<div class="text-[11px] text-hud-red px-2 py-2">Không tải được nhóm page.</div>`;
            pageList.innerHTML = `<div class="text-[11px] text-hud-red px-2 py-2">Không tải được fanpage.</div>`;
        }
    }

    function renderFacebookCreateImagePreview() {
        const preview = document.getElementById("fb-create-image-preview");
        if (!preview) return;
        const images = state.facebookCreateImages || [];
        if (!images.length) {
            preview.classList.add("hidden");
            preview.innerHTML = "";
            renderFacebookCreateSummary();
            return;
        }
        preview.classList.remove("hidden");
        const hero = images[0];
        const rest = images.slice(1);
        preview.innerHTML = `
            <div class="flex items-center justify-between gap-3 mb-3">
                <div>
                    <div class="text-[11px] text-white font-bold">Ảnh đã chọn</div>
                    <div class="text-[10px] text-hud-muted">${formatNumber(images.length)} ảnh sẽ được đưa vào bài đăng</div>
                </div>
                <button type="button" class="fb-create-image-clear text-[10px] text-hud-red border border-hud-red/30 px-2 py-1 hover:bg-hud-red/10">
                    <i class="fa-solid fa-trash"></i> XÓA HẾT
                </button>
            </div>
            <div class="rounded-2xl overflow-hidden border border-white/10 bg-black/40">
                <div class="${rest.length ? "grid grid-cols-2 gap-1" : ""}">
                    <div class="relative ${rest.length ? "min-h-[260px]" : ""}">
                        <img src="${escapeHtml(hero.data_url)}" alt="${escapeHtml(hero.name)}" class="w-full ${rest.length ? "h-full absolute inset-0" : "max-h-[420px]"} object-cover"/>
                        <button type="button" class="fb-create-image-remove absolute top-2 right-2 w-7 h-7 rounded-full bg-black/75 text-white hover:text-hud-red" data-index="0"><i class="fa-solid fa-xmark"></i></button>
                    </div>
                    ${rest.length ? `
                        <div class="grid ${rest.length === 1 ? "grid-cols-1" : "grid-cols-2"} gap-1">
                            ${rest.map((image, offset) => {
                                const index = offset + 1;
                                return `
                                    <div class="relative min-h-[128px]">
                                        <img src="${escapeHtml(image.data_url)}" alt="${escapeHtml(image.name)}" class="absolute inset-0 w-full h-full object-cover"/>
                                        <button type="button" class="fb-create-image-remove absolute top-2 right-2 w-7 h-7 rounded-full bg-black/75 text-white hover:text-hud-red" data-index="${index}"><i class="fa-solid fa-xmark"></i></button>
                                    </div>
                                `;
                            }).join("")}
                        </div>
                    ` : ""}
                </div>
            </div>
        `;
        preview.querySelector(".fb-create-image-clear")?.addEventListener("click", () => {
            state.facebookCreateImages = [];
            const input = document.getElementById("fb-create-images");
            if (input) input.value = "";
            renderFacebookCreateImagePreview();
        });
        preview.querySelectorAll(".fb-create-image-remove").forEach((button) => {
            button.addEventListener("click", () => {
                state.facebookCreateImages.splice(Number(button.dataset.index || 0), 1);
                const input = document.getElementById("fb-create-images");
                if (input && !state.facebookCreateImages.length) input.value = "";
                renderFacebookCreateImagePreview();
            });
        });
        renderFacebookCreateSummary();
    }

    async function readFacebookCreateImages(files) {
        const selected = Array.from(files || []).filter((file) => file.type.startsWith("image/")).slice(0, 6);
        const uploadResult = await uploadFacebookCreateImages(selected);
        const uploadedByName = new Map((uploadResult.images || []).map((item) => [item.name, item]));
        if ((uploadResult.warnings || []).length) {
            setFacebookCreateFeedback("warn", uploadResult.warnings.join(" | "));
        }
        const images = [];
        for (const file of selected) {
            const dataUrl = await new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onload = () => resolve(String(reader.result || ""));
                reader.onerror = reject;
                reader.readAsDataURL(file);
            });
            const uploaded = uploadedByName.get(file.name) || {};
            if (!uploaded.image_id) continue;
            images.push({
                image_id: uploaded.image_id,
                name: file.name,
                type: uploaded.type || file.type,
                size: file.size,
                stored_size: uploaded.stored_size || 0,
                data_url: dataUrl,
            });
        }
        state.facebookCreateImages = images;
        state.facebookCreatePreview = null;
        renderFacebookCreateImagePreview();
    }

    function facebookCreatePayload() {
        const brief = document.getElementById("fb-create-brief")?.value.trim() || "";
        const pageIds = Array.from(document.querySelectorAll(".fb-create-page-checkbox:checked")).map((input) => input.value.trim()).filter(Boolean);
        const groups = state.selectedFacebookCreateGroups || [];
        const hashtagCount = Number.parseInt(document.getElementById("fb-create-hashtag-count")?.value || "5", 10);
        const scheduleMode = document.querySelector('input[name="schedule"]:checked')?.value || "now";
        return {
            brief,
            page_ids: pageIds,
            groups,
            tone: document.getElementById("fb-create-tone")?.value || "",
            hashtag_count: Number.isFinite(hashtagCount) ? Math.max(0, Math.min(12, hashtagCount)) : 5,
            schedule_mode: scheduleMode,
            scheduled_at: state.facebookCreateScheduledAt ? new Date(state.facebookCreateScheduledAt).toISOString() : "",
            scheduled_at_local: state.facebookCreateScheduledAt || "",
            images: (state.facebookCreateImages || []).map((image) => ({
                image_id: image.image_id || "",
                name: image.name || "",
                type: image.type || "",
                size: Number(image.size || 0),
            })),
        };
    }

    function toDatetimeLocalValue(date) {
        const pad = (value) => String(value).padStart(2, "0");
        return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
    }

    function nextFacebookScheduleTimeFromHour(hourMinute) {
        const match = String(hourMinute || "").match(/^(\d{1,2}):(\d{2})/);
        const now = new Date();
        const target = new Date(now);
        target.setHours(match ? Number(match[1]) : 20, match ? Number(match[2]) : 0, 0, 0);
        if (target.getTime() <= now.getTime() + 10 * 60 * 1000) {
            target.setDate(target.getDate() + 1);
        }
        return target;
    }

    function renderFacebookScheduleDialog(defaultValue = "") {
        const fallback = new Date(Date.now() + 30 * 60 * 1000);
        const value = defaultValue || state.facebookCreateScheduledAt || toDatetimeLocalValue(fallback);
        return `<div id="fb-schedule-dialog" class="fixed inset-0 bg-black/75 backdrop-blur-md flex items-center justify-center p-4" style="z-index:9999;">
            <div class="hud-card w-full max-w-md p-0 relative overflow-hidden" style="border-color: rgba(74, 158, 255, 0.45); box-shadow: 0 0 40px rgba(74, 158, 255, 0.14);">
                <span class="c-tl" style="border-color:#4a9eff;"></span><span class="c-br" style="border-color:#4a9eff;"></span>
                <div class="header-strip px-5 py-4 flex items-center gap-3" style="background: linear-gradient(90deg, rgba(74, 158, 255, 0.18), rgba(0, 240, 255, 0.04));">
                    <i class="fa-solid fa-clock text-hud-fb"></i>
                    <div>
                        <div class="font-display text-white text-sm font-black uppercase-widest">Hẹn giờ đăng bài</div>
                        <div class="text-[10px] text-hud-muted">Chọn ngày giờ để Facebook scheduled post.</div>
                    </div>
                    <button id="fb-schedule-close" class="ml-auto h-9 w-9 border border-white/10 bg-black/30 text-hud-muted hover:text-white hover:border-hud-fb/50 text-xs"><i class="fa-solid fa-xmark"></i></button>
                </div>
                <form id="fb-schedule-form" class="p-5 space-y-4">
                    <div>
                        <label class="block text-[10px] uppercase-wide text-hud-fb mb-2">Ngày giờ đăng</label>
                        <input id="fb-schedule-datetime" type="datetime-local" class="hud-input w-full px-4 py-3 text-sm" value="${escapeHtml(value)}">
                        <div class="text-[10px] text-hud-muted mt-2">Nên chọn thời điểm sau hiện tại ít nhất 10 phút.</div>
                    </div>
                    <div class="flex justify-end gap-2 pt-2">
                        <button id="fb-schedule-cancel" type="button" class="btn-ghost px-4 py-2 text-[10px] uppercase-wide font-bold">Hủy</button>
                        <button type="submit" class="px-5 py-2 text-[10px] uppercase-wide font-bold" style="background:#4a9eff;color:white;border:1px solid #4a9eff;"><i class="fa-solid fa-check"></i> Chọn giờ</button>
                    </div>
                </form>
            </div>
        </div>`;
    }

    function openFacebookScheduleDialog(defaultValue = "") {
        document.getElementById("fb-schedule-dialog")?.remove();
        document.body.insertAdjacentHTML("beforeend", renderFacebookScheduleDialog(defaultValue));
    }

    function closeFacebookScheduleDialog() {
        document.getElementById("fb-schedule-dialog")?.remove();
    }

    async function applyFacebookBestTimeSchedule() {
        try {
            const stats = await fetchJSON("/facebook/stats?days=7");
            const bestTime = stats.best_posting_time || "20:00";
            const target = nextFacebookScheduleTimeFromHour(bestTime);
            state.facebookCreateScheduledAt = toDatetimeLocalValue(target);
            setFacebookCreateFeedback("success", `AI đã chọn giờ vàng: ${state.facebookCreateScheduledAt.replace("T", " ")}.`);
        } catch (error) {
            const target = nextFacebookScheduleTimeFromHour("20:00");
            state.facebookCreateScheduledAt = toDatetimeLocalValue(target);
            setFacebookCreateFeedback("warn", `Không lấy được giờ vàng từ stats, tạm dùng ${state.facebookCreateScheduledAt.replace("T", " ")}.`);
        }
    }

    function setFacebookCreateFeedback(kind, message) {
        const feedback = document.getElementById("fb-create-feedback");
        if (!feedback) return;
        const tone = {
            error: "text-hud-red border-hud-red/30 bg-hud-red/10",
            success: "text-hud-green border-hud-green/30 bg-hud-green/10",
            loading: "text-hud-cyan border-hud-cyan/30 bg-hud-cyan/10",
            warn: "text-hud-amber border-hud-amber/30 bg-hud-amber/10",
        }[kind] || "text-hud-muted border-hud-fb/20 bg-black/20";
        feedback.className = `text-[11px] border p-3 ${tone}`;
        feedback.textContent = message;
        feedback.classList.remove("hidden");
    }

    function renderFacebookContentVariantPreview(result) {
        const container = document.getElementById("fb-create-variant-preview");
        if (!container) return;
        const coreCaptions = result.core_captions || [];
        const posts = result.posts || [];
        const review = result.quality?.review || {};
        state.facebookCreatePreview = result;
        container.classList.remove("hidden");
        container.innerHTML = `
            <div class="hud-card p-4 mt-2" style="border-color: rgba(74, 158, 255, 0.25);">
                <span class="c-tl" style="border-color:#4a9eff;"></span><span class="c-br" style="border-color:#4a9eff;"></span>
                <div class="flex flex-wrap items-center gap-2 mb-4">
                    <div class="font-display font-black text-xs text-white uppercase-widest"><i class="fa-solid fa-wand-magic-sparkles text-hud-fb"></i> PREVIEW CONTENT SPIN</div>
                    <span class="badge cyan ml-auto">${formatNumber(result.page_count || 0)} page</span>
                    <span class="badge green">${formatNumber(result.core_caption_count || 0)} lõi</span>
                    ${review.enabled ? `<span class="badge cyan">review ${formatNumber(review.reviewed || 0)}</span>` : ""}
                    ${review.rewritten ? `<span class="badge amber">rewrite ${formatNumber(review.rewritten || 0)}</span>` : ""}
                    <span class="badge amber">sim ${formatNumber(result.quality?.max_nearby_similarity || 0, 2)}</span>
                </div>
                ${(result.warnings || []).length ? `<div class="mb-4 border border-hud-amber/30 bg-hud-amber/10 text-hud-amber text-[11px] p-3">${escapeHtml((result.warnings || []).join(" | "))}</div>` : ""}
                <div class="grid grid-cols-2 gap-4 mb-5">
                    <div>
                        <div class="text-[10px] text-hud-muted uppercase-wide mb-2">CAPTION LÕI</div>
                        <div class="space-y-2 max-h-72 overflow-y-auto pr-1">
                            ${coreCaptions.map((item, index) => `
                                <div class="border border-hud-fb/15 bg-black/25 p-3">
                                    <div class="text-[10px] text-hud-fb uppercase-wide mb-1">#${index + 1} · ${escapeHtml(item.angle || "angle")}</div>
                                    <div class="text-[12px] text-white font-black mb-1">${escapeHtml(item.headline || "")}</div>
                                    <div class="text-[11px] text-white/85 line-clamp-3">${escapeHtml(item.caption || "")}</div>
                                    ${item.cta ? `<div class="text-[10px] text-hud-green mt-2"><i class="fa-solid fa-bullhorn"></i> ${escapeHtml(item.cta)}</div>` : ""}
                                </div>
                            `).join("") || `<div class="text-[11px] text-hud-muted">Chưa có caption lõi.</div>`}
                        </div>
                    </div>
                    <div>
                        <div class="text-[10px] text-hud-muted uppercase-wide mb-2">POST THEO PAGE</div>
                        <div class="space-y-3 max-h-[520px] overflow-y-auto pr-1">
                            ${posts.map((post, index) => {
                                return `
                                    <div class="rounded-2xl border border-hud-fb/20 bg-[#101923] p-4">
                                        <div class="flex items-start gap-3 mb-3">
                                            <div class="w-9 h-9 rounded-full bg-hud-fb/10 border border-hud-fb/30 flex items-center justify-center flex-shrink-0">
                                                <i class="fa-brands fa-facebook text-hud-fb text-xs"></i>
                                            </div>
                                            <div class="flex-1 min-w-0">
                                                <div class="text-white text-[12px] font-bold truncate">${escapeHtml(post.page_name || post.page_id || "Facebook page")}</div>
                                                <div class="text-[10px] text-hud-muted truncate">
                                                    ${escapeHtml(post.group || "Chưa có nhóm")} · core #${Number(post.core_index || 0) + 1}
                                                    ${post.review?.rewritten ? `<span class="text-hud-amber"> · đã rewrite</span>` : ""}
                                                </div>
                                            </div>
                                            <button type="button" class="fb-preview-edit btn-ghost px-2 py-1 text-[10px]" data-index="${index}" title="Chỉnh sửa"><i class="fa-solid fa-pen"></i></button>
                                            <button type="button" class="fb-preview-copy btn-ghost px-2 py-1 text-[10px]" data-index="${index}"><i class="fa-solid fa-copy"></i></button>
                                        </div>
                                        <div class="fb-preview-display whitespace-pre-wrap text-[12px] leading-relaxed text-white/90" data-index="${index}">${escapeHtml(post.caption || "")}</div>
                                        <div class="fb-preview-editor hidden" data-index="${index}">
                                            <textarea class="fb-preview-caption hud-textarea w-full min-h-[220px] px-3 py-3 text-[12px] leading-relaxed" data-index="${index}">${escapeHtml(post.caption || "")}</textarea>
                                            <div class="flex justify-end gap-2 mt-2">
                                                <button type="button" class="fb-preview-cancel btn-ghost px-3 py-1.5 text-[10px] uppercase-wide" data-index="${index}">HỦY</button>
                                                <button type="button" class="fb-preview-save btn-primary px-3 py-1.5 text-[10px] uppercase-wide" data-index="${index}">LƯU</button>
                                            </div>
                                        </div>
                                        ${(post.hashtags || []).length ? `<div class="mt-3 flex flex-wrap gap-1">${(post.hashtags || []).map((tag) => `<span class="badge cyan">${escapeHtml(tag)}</span>`).join("")}</div>` : ""}
                                    </div>
                                `;
                            }).join("") || `<div class="text-[11px] text-hud-muted">Chưa có post preview.</div>`}
                        </div>
                    </div>
                </div>
            </div>
        `;
        container.querySelectorAll(".fb-preview-copy").forEach((button) => {
            button.addEventListener("click", async () => {
                const post = posts[Number(button.dataset.index || 0)];
                const text = [post?.caption || "", ...(post?.hashtags || [])].filter(Boolean).join("\n\n");
                await copyTextToClipboard(text);
                button.innerHTML = `<i class="fa-solid fa-check"></i>`;
                setTimeout(() => {
                    button.innerHTML = `<i class="fa-solid fa-copy"></i>`;
                }, 1200);
            });
        });
        container.querySelectorAll(".fb-preview-edit").forEach((button) => {
            button.addEventListener("click", () => {
                const index = button.dataset.index || "0";
                const display = container.querySelector(`.fb-preview-display[data-index="${index}"]`);
                const editor = container.querySelector(`.fb-preview-editor[data-index="${index}"]`);
                const textarea = container.querySelector(`.fb-preview-caption[data-index="${index}"]`);
                const post = state.facebookCreatePreview?.posts?.[Number(index)];
                if (textarea && post) textarea.value = post.caption || "";
                display?.classList.add("hidden");
                editor?.classList.remove("hidden");
                textarea?.focus();
            });
        });
        container.querySelectorAll(".fb-preview-cancel").forEach((button) => {
            button.addEventListener("click", () => {
                const index = button.dataset.index || "0";
                const display = container.querySelector(`.fb-preview-display[data-index="${index}"]`);
                const editor = container.querySelector(`.fb-preview-editor[data-index="${index}"]`);
                editor?.classList.add("hidden");
                display?.classList.remove("hidden");
            });
        });
        container.querySelectorAll(".fb-preview-save").forEach((button) => {
            button.addEventListener("click", () => {
                const index = Number(button.dataset.index || 0);
                const textarea = container.querySelector(`.fb-preview-caption[data-index="${index}"]`);
                const display = container.querySelector(`.fb-preview-display[data-index="${index}"]`);
                const editor = container.querySelector(`.fb-preview-editor[data-index="${index}"]`);
                if (!state.facebookCreatePreview?.posts?.[index]) return;
                const value = textarea?.value || "";
                state.facebookCreatePreview.posts[index].caption = value;
                const firstLine = value.split(/\r?\n/).find((line) => line.trim()) || "";
                state.facebookCreatePreview.posts[index].headline = firstLine.trim().slice(0, 180);
                if (display) display.textContent = value;
                editor?.classList.add("hidden");
                display?.classList.remove("hidden");
            });
        });
    }

    async function previewFacebookCreateVariants() {
        const payload = facebookCreatePayload();
        if (!payload.brief) {
            setFacebookCreateFeedback("error", "Cần nhập content brief trước khi preview.");
            return;
        }
        if (!payload.page_ids.length && !payload.groups.length) {
            setFacebookCreateFeedback("error", "Cần chọn ít nhất một page hoặc nhóm page để preview.");
            return;
        }
        const button = document.getElementById("fb-create-preview");
        if (button) {
            button.disabled = true;
            button.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> PREVIEWING`;
        }
        setFacebookCreateFeedback("loading", "Đang dùng LLM writer để sinh caption lõi và preview theo từng page...");
        try {
            const result = await fetchJSON("/facebook/content/preview-variants", {
                method: "POST",
                body: JSON.stringify(payload),
            });
            renderFacebookContentVariantPreview(result);
            setFacebookCreateFeedback("success", `Đã tạo preview: ${formatNumber(result.core_caption_count || 0)} caption lõi cho ${formatNumber(result.page_count || 0)} page.`);
        } catch (error) {
            setFacebookCreateFeedback("error", `Preview failed: ${error.message}`);
        } finally {
            if (button) {
                button.disabled = false;
                button.innerHTML = `<i class="fa-solid fa-eye"></i> PREVIEW`;
            }
        }
    }

    async function uploadFacebookCreateImages(files) {
        const selected = Array.from(files || []).filter((file) => file.type.startsWith("image/")).slice(0, 10);
        if (!selected.length) return [];
        const formData = new FormData();
        selected.forEach((file) => formData.append("files", file));
        const response = await fetch(`${API_BASE}/facebook/content/images`, {
            method: "POST",
            body: formData,
        });
        if (!response.ok) {
            let message = `${response.status} ${response.statusText}`;
            try {
                const data = await response.json();
                message = data.detail || data.message || message;
            } catch (_) {}
            throw new Error(message);
        }
        return response.json();
    }

    function updateSiteSummary(summary, selectedIds = [], sites = []) {
        if (!summary) return;
        const selected = selectedIds || [];
        if (!selected.length) {
            summary.textContent = "Chọn website đích";
            summary.className = "flex-1 text-hud-muted";
            return;
        }
        const selectedSites = sites.filter((site) => selected.includes(site.site_id));
        if (selectedSites.length === 1) {
            summary.textContent = selectedSites[0].site_name || selectedSites[0].url;
        } else {
            summary.textContent = `${selectedSites.length} sites selected`;
        }
        summary.className = "flex-1 text-white";
    }

    async function submitJob() {
        const urlInput = document.getElementById("submit-urls");
        const publishInput = document.querySelector('input[name="status"]:checked');
        const contentModeInput = document.querySelector('input[name="content-mode"]:checked');
        if (!urlInput) return;
        const urls = urlInput.value
            .split(/\r?\n/)
            .map((item) => item.trim())
            .filter(Boolean);
        const siteIds = Array.from(document.querySelectorAll(".submit-site-checkbox:checked")).map((input) => input.value.trim()).filter(Boolean);
        if (!siteIds.length) {
            showFeedback("error", "Cần chọn ít nhất một website đích.");
            return;
        }
        if (!urls.length) {
            showFeedback("error", "Cần ít nhất một URL.");
            return;
        }
        setSelectedSubmitSites(siteIds);
        const enqueueButton = document.getElementById("submit-enqueue");
        if (enqueueButton) enqueueButton.disabled = true;
        try {
            const payload = await fetchJSON("/submit-batch", {
                method: "POST",
                body: JSON.stringify({
                    urls,
                    site_ids: siteIds,
                    content_mode: contentModeInput?.value || "shared",
                    woo_category_id: 1,
                    priority: "normal",
                    publish_status: publishInput?.value || "draft",
                }),
            });
            const focusJobId = (payload.master_job_ids || [])[0] || (payload.child_job_ids || [])[0];
            if (focusJobId) {
                setSelectedJob(focusJobId);
            }
            showFeedback("success", `Đã tạo batch ${payload.batch_id} với ${payload.total_jobs} job.`);
            await renderRecentSubmissions();
            if (window.switchPage) {
                window.switchPage("jobs");
            }
        } catch (error) {
            showFeedback("error", `Submit failed: ${error.message}`);
        } finally {
            if (enqueueButton) enqueueButton.disabled = false;
        }
    }

    function shopeePrice(price, currency = "VND") {
        const amount = Number(price || 0);
        if (!Number.isFinite(amount) || amount <= 0) return "-";
        return `${formatNumber(amount)} ${currency}`;
    }

    function shopeeTypeBadge(type) {
        return String(type || "").toLowerCase() === "variable" ? "amber" : "green";
    }

    async function renderShopeePage() {
        const section = document.getElementById("page-shopee");
        if (!section) return;
        section.innerHTML = `<div class="max-w-7xl mx-auto text-hud-muted text-sm">Loading Shopee products...</div>`;
        try {
            const [productsPayload] = await Promise.all([fetchJSON("/shopee/products?limit=100")]);
            const items = productsPayload.items || [];
            setSelectedShopeeItem("");
            section.innerHTML = `
                <div class="max-w-7xl mx-auto overflow-x-hidden">
                    <div class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4 mb-6">
                        <div class="hud-card p-4"><span class="c-tl"></span><span class="c-br"></span><div class="text-[9px] text-hud-cyan uppercase-widest mb-1">RAW PRODUCTS</div><div class="metric-num text-2xl text-white">${items.length}</div></div>
                        <div class="hud-card amber p-4"><span class="c-tl"></span><span class="c-br"></span><div class="text-[9px] text-hud-amber uppercase-widest mb-1">VARIABLE</div><div class="metric-num text-2xl text-hud-amber">${items.filter((item) => item.type === "variable").length}</div></div>
                        <div class="hud-card green p-4"><span class="c-tl"></span><span class="c-br"></span><div class="text-[9px] text-hud-green uppercase-widest mb-1">SIMPLE</div><div class="metric-num text-2xl text-hud-green">${items.filter((item) => item.type === "simple").length}</div></div>
                        <div class="hud-card p-4"><span class="c-tl"></span><span class="c-br"></span><div class="text-[9px] text-hud-cyan uppercase-widest mb-1">SOURCE</div><div class="text-[11px] text-white truncate">${escapeHtml(productsPayload.category_label || "Shopee normalized catalog")}</div><div class="text-[10px] text-hud-muted truncate mt-1">${escapeHtml(productsPayload.source_url || "chrome-extension")}</div></div>
                    </div>
                    <div class="hud-card fade-in">
                        <span class="c-tl"></span><span class="c-br"></span>
                        <div class="header-strip px-5 py-3 flex items-center gap-2">
                            <i class="fa-solid fa-store text-hud-amber"></i>
                            <span class="font-display font-black text-xs text-white uppercase-widest">NORMALIZED PRODUCTS</span>
                        </div>
                        <div class="p-5">
                            <div class="space-y-3">
                                ${items.map((item) => {
                                    const imageUrl = item.image_url || "";
                                    return `
                                        <label class="shopee-row flex items-center gap-4 border border-hud-cyan/12 bg-black/20 p-3 hover:border-hud-cyan/30 transition cursor-pointer min-w-0" data-item-id="${escapeHtml(item.item_id)}">
                                            <input type="checkbox" class="shopee-item-checkbox accent-cyan-400 shrink-0"/>
                                            <div class="w-16 h-16 border border-hud-cyan/20 bg-black/30 flex items-center justify-center overflow-hidden shrink-0">
                                                ${imageUrl ? `<img src="${escapeHtml(imageUrl)}" alt="${escapeHtml(item.title)}" class="w-full h-full object-cover"/>` : `<i class="fa-solid fa-image text-hud-cyan/40"></i>`}
                                            </div>
                                            <div class="min-w-0 flex-1">
                                                <div class="text-white font-bold leading-5 break-words">${escapeHtml(truncate(item.title, 160))}</div>
                                                <div class="mt-1 flex flex-wrap gap-2">
                                                    <span class="badge ${shopeeTypeBadge(item.type)}">${escapeHtml(item.type)}</span>
                                                    <span class="badge cyan">${escapeHtml(shopeePrice(item.regular_price))}</span>
                                                    <span class="badge purple">${escapeHtml(`${item.variant_count || 0} variants`)}</span>
                                                </div>
                                                <div class="text-[10px] text-hud-muted truncate mt-2">${escapeHtml(item.url || item.item_id)}</div>
                                            </div>
                                            <div class="text-[10px] text-hud-muted text-right shrink-0 hidden md:block">${escapeHtml(formatDate(item.updated_at))}</div>
                                        </label>
                                    `;
                                }).join("") || `<div class="text-center py-6 text-hud-muted">No normalized Shopee products yet. Push data from the Chrome extension API first.</div>`}
                            </div>
                        </div>
                    </div>
                </div>
                <div id="shopee-drawer-backdrop" class="hidden fixed inset-0 bg-black/60 z-40 opacity-0 transition-opacity duration-300"></div>
                <aside id="shopee-normalize-drawer" class="hidden fixed right-0 top-0 h-full w-full max-w-[440px] bg-[#07141c] border-l border-hud-cyan/20 shadow-[-24px_0_60px_rgba(0,0,0,0.45)] z-50 overflow-y-auto translate-x-full opacity-0 transition-all duration-300 ease-out">
                    <div class="header-strip px-5 py-4 flex items-center gap-2 sticky top-0">
                        <i class="fa-solid fa-wand-magic-sparkles text-hud-cyan"></i>
                        <span class="font-display font-black text-xs text-white uppercase-widest">NORMALIZE TO WOO</span>
                        <button id="shopee-drawer-close" class="ml-auto text-hud-cyan hover:text-white text-sm"><i class="fa-solid fa-xmark"></i></button>
                    </div>
                    <div class="p-5 space-y-4">
                        <div>
                            <div class="text-[10px] font-bold text-hud-cyan uppercase-widest mb-2">Selected Product</div>
                            <div id="shopee-selected-product" class="text-[11px] text-white leading-6">-</div>
                        </div>
                        <div>
                            <label class="text-[10px] font-bold text-hud-cyan uppercase-widest mb-2 block">Target Sites</label>
                            <div class="relative">
                                <button id="shopee-site-trigger" type="button" class="hud-input w-full px-4 py-3 text-sm text-left flex items-center gap-3">
                                    <span id="shopee-site-summary" class="flex-1 text-hud-muted">Đang tải website...</span>
                                    <i class="fa-solid fa-chevron-down text-hud-cyan text-xs"></i>
                                </button>
                                <div id="shopee-site-dropdown" class="hidden absolute left-0 right-0 top-full mt-2 z-20 border border-hud-cyan/30 bg-[#07141c] shadow-[0_18px_60px_rgba(0,0,0,0.45)]">
                                    <div id="shopee-site-list" class="max-h-72 overflow-y-auto p-2 space-y-2">
                                        <div class="text-[11px] text-hud-muted px-2 py-2">Đang tải website...</div>
                                    </div>
                                </div>
                            </div>
                            <p id="shopee-site-help" class="text-[10px] text-hud-muted mt-2">Chọn website đích để tạo job từ dữ liệu Shopee.</p>
                        </div>
                        <div class="space-y-3">
                            <label class="flex items-center gap-2 cursor-pointer"><input type="radio" name="shopee-content-mode" value="shared" class="hud-radio" checked/><span class="text-xs text-hud-green font-bold uppercase-wide">ONE CONTENT / MANY SITES</span></label>
                            <label class="flex items-center gap-2 cursor-pointer"><input type="radio" name="shopee-content-mode" value="per-site" class="hud-radio"/><span class="text-xs text-hud-muted font-bold uppercase-wide">REWRITE PER SITE</span></label>
                        </div>
                        <div class="flex gap-4">
                            <label class="flex items-center gap-2 cursor-pointer"><input type="radio" name="shopee-status" value="publish" class="hud-radio" checked/><span class="text-xs text-hud-green font-bold uppercase-wide">PUBLISH</span></label>
                            <label class="flex items-center gap-2 cursor-pointer"><input type="radio" name="shopee-status" value="draft" class="hud-radio"/><span class="text-xs text-hud-muted font-bold uppercase-wide">DRAFT</span></label>
                        </div>
                        <div id="shopee-feedback" class="text-[11px] text-hud-muted"></div>
                        <button id="shopee-enqueue" class="btn-primary w-full py-3 text-xs uppercase-wide font-bold tracking-widest flex items-center justify-center gap-2" disabled>
                            <i class="fa-solid fa-paper-plane"></i> ENQUEUE FROM RAW PRODUCT
                        </button>
                    </div>
                </aside>
            `;
            await renderSiteMultiSelect({
                listId: "shopee-site-list",
                helpId: "shopee-site-help",
                summaryId: "shopee-site-summary",
                checkboxClass: "shopee-site-checkbox",
                selectedIds: state.selectedShopeeSiteIds,
                setSelectedIds: setSelectedShopeeSites,
                emptyHelpText: "Chưa có site nào. Tạo site ở màn Quản Lý Website trước.",
                loadedHelpText: "Có thể chọn nhiều site để tạo batch job từ cùng một raw product.",
            });
            const itemMap = new Map(items.map((item) => [item.item_id, item]));
            const drawer = section.querySelector("#shopee-normalize-drawer");
            const backdrop = section.querySelector("#shopee-drawer-backdrop");
            const shopeeTrigger = section.querySelector("#shopee-site-trigger");
            const shopeeDropdown = section.querySelector("#shopee-site-dropdown");
            const selectedProductEl = section.querySelector("#shopee-selected-product");
            const enqueueButton = section.querySelector("#shopee-enqueue");
            const feedback = section.querySelector("#shopee-feedback");
            const animateDrawerOpen = () => {
                if (!drawer || !backdrop) return;
                drawer.classList.remove("hidden");
                backdrop.classList.remove("hidden");
                requestAnimationFrame(() => {
                    drawer.classList.remove("translate-x-full", "opacity-0");
                    backdrop.classList.remove("opacity-0");
                });
            };
            const animateDrawerClose = () => {
                if (!drawer || !backdrop) return;
                drawer.classList.add("translate-x-full", "opacity-0");
                backdrop.classList.add("opacity-0");
                window.setTimeout(() => {
                    if (!state.selectedShopeeItemId) {
                        drawer.classList.add("hidden");
                        backdrop.classList.add("hidden");
                    }
                }, 300);
            };
            const closeDrawer = () => {
                setSelectedShopeeItem("");
                if (selectedProductEl) selectedProductEl.textContent = "-";
                if (enqueueButton) enqueueButton.disabled = true;
                if (feedback) feedback.textContent = "";
                section.querySelectorAll(".shopee-item-checkbox").forEach((checkbox) => {
                    checkbox.checked = false;
                });
                section.querySelectorAll(".shopee-row").forEach((row) => {
                    row.classList.remove("border-hud-cyan/50", "bg-hud-cyan/10");
                    row.classList.add("border-hud-cyan/12", "bg-black/20");
                });
                animateDrawerClose();
            };
            const openDrawer = async (itemId) => {
                const item = itemMap.get(itemId);
                if (!item || !drawer || !backdrop) return;
                setSelectedShopeeItem(itemId);
                section.querySelectorAll(".shopee-item-checkbox").forEach((checkbox) => {
                    checkbox.checked = checkbox.closest(".shopee-row")?.dataset.itemId === itemId;
                });
                section.querySelectorAll(".shopee-row").forEach((row) => {
                    const active = row.dataset.itemId === itemId;
                    row.classList.toggle("border-hud-cyan/50", active);
                    row.classList.toggle("bg-hud-cyan/10", active);
                    row.classList.toggle("border-hud-cyan/12", !active);
                    row.classList.toggle("bg-black/20", !active);
                });
                if (selectedProductEl) {
                    selectedProductEl.innerHTML = `
                        <div class="font-bold text-white leading-6">${escapeHtml(item.title || "-")}</div>
                        <div class="mt-2 flex flex-wrap gap-2">
                            <span class="badge ${shopeeTypeBadge(item.type)}">${escapeHtml(item.type)}</span>
                            <span class="badge cyan">${escapeHtml(shopeePrice(item.regular_price))}</span>
                            <span class="badge purple">${escapeHtml(`${item.variant_count || 0} variants`)}</span>
                        </div>
                    `;
                }
                if (enqueueButton) enqueueButton.disabled = false;
                animateDrawerOpen();
            };
            shopeeTrigger?.addEventListener("click", (event) => {
                event.preventDefault();
                shopeeDropdown?.classList.toggle("hidden");
            });
            section.addEventListener("click", (event) => {
                if (!shopeeDropdown || !shopeeTrigger) return;
                if (shopeeDropdown.classList.contains("hidden")) return;
                if (shopeeDropdown.contains(event.target) || shopeeTrigger.contains(event.target)) return;
                shopeeDropdown.classList.add("hidden");
            });
            section.querySelector("#shopee-drawer-close")?.addEventListener("click", closeDrawer);
            backdrop?.addEventListener("click", closeDrawer);
            section.querySelectorAll(".shopee-row").forEach((row) => row.addEventListener("click", (event) => {
                const checkbox = row.querySelector(".shopee-item-checkbox");
                const nextId = row.dataset.itemId || "";
                if (event.target instanceof HTMLInputElement && event.target.classList.contains("shopee-item-checkbox")) {
                    if (!event.target.checked) {
                        closeDrawer();
                        return;
                    }
                }
                if (checkbox) checkbox.checked = true;
                openDrawer(nextId);
            }));
            enqueueButton?.addEventListener("click", async () => {
                const siteIds = Array.from(section.querySelectorAll(".shopee-site-checkbox:checked")).map((input) => input.value.trim()).filter(Boolean);
                const selectedItemId = state.selectedShopeeItemId;
                if (!selectedItemId) {
                    if (feedback) feedback.textContent = "Chưa chọn sản phẩm Shopee.";
                    return;
                }
                if (!siteIds.length) {
                    if (feedback) feedback.textContent = "Cần chọn ít nhất một website đích.";
                    return;
                }
                try {
                    if (feedback) feedback.textContent = "Đang tạo batch job...";
                    const payload = await fetchJSON(`/shopee/products/${encodeURIComponent(selectedItemId)}/enqueue`, {
                        method: "POST",
                        body: JSON.stringify({
                            site_ids: siteIds,
                            content_mode: section.querySelector('input[name="shopee-content-mode"]:checked')?.value || "shared",
                            publish_status: section.querySelector('input[name="shopee-status"]:checked')?.value || "draft",
                            woo_category_id: 1,
                            priority: "normal",
                        }),
                    });
                    const focusJobId = (payload.master_job_ids || [])[0] || (payload.child_job_ids || [])[0];
                    if (focusJobId) setSelectedJob(focusJobId);
                    if (feedback) feedback.textContent = `Đã tạo batch ${payload.batch_id} với ${payload.total_jobs} job.`;
                    if (window.switchPage) window.switchPage("jobs");
                } catch (error) {
                    if (feedback) feedback.textContent = `Enqueue failed: ${error.message}`;
                }
            });
        } catch (error) {
            section.innerHTML = `<div class="max-w-7xl mx-auto text-hud-red text-sm">Failed to load Shopee products: ${escapeHtml(error.message)}</div>`;
        }
    }

    function jobsMetrics(jobs, stats) {
        const processing = jobs.filter((job) => job.status === "processing").length;
        const queued = jobs.filter((job) => job.status === "pending").length;
        const completed = jobs.filter((job) => job.status === "completed").length;
        const failed = jobs.filter((job) => job.status === "failed").length;
        return { processing, queued, completed, failed, avgTime: stats.avg_processing_time_sec || 0 };
    }

    function normalizeJobsStatusFilter(status) {
        const key = String(status || "").toLowerCase();
        if (key === "queued") return "pending";
        if (["processing", "pending", "completed", "failed"].includes(key)) return key;
        return "";
    }

    function filterJobsByStatus(jobs) {
        const filter = normalizeJobsStatusFilter(state.jobsStatusFilter);
        if (!filter) return jobs;
        return jobs.filter((job) => String(job.status || "").toLowerCase() === filter);
    }

    function jobSortValue(job, key) {
        if (key === "job_id") return String(job.job_id || "");
        if (key === "title") return String(job.title || job.url || "");
        if (key === "step") return PAGE_STEPS.indexOf(job.current_step) >= 0 ? PAGE_STEPS.indexOf(job.current_step) : 999;
        if (key === "status") {
            const rank = { pending: 1, processing: 2, completed: 3, failed: 4, duplicate: 5 };
            return rank[String(job.status || "").toLowerCase()] || 99;
        }
        return "";
    }

    function sortJobs(jobs) {
        const key = state.jobsSortKey;
        if (!key) return jobs;
        const dir = state.jobsSortDir === "desc" ? -1 : 1;
        return [...jobs].sort((left, right) => {
            const a = jobSortValue(left, key);
            const b = jobSortValue(right, key);
            if (typeof a === "number" && typeof b === "number") return (a - b) * dir;
            return String(a).localeCompare(String(b), "vi", { numeric: true, sensitivity: "base" }) * dir;
        });
    }

    function progressDots(job) {
        const activeIndex = PAGE_STEPS.indexOf(job.current_step);
        return PAGE_STEPS.slice(0, 10).map((step, index) => {
            let cls = "bg-hud-cyan/20";
            if (job.status === "duplicate" && index === 0) cls = "bg-hud-purple";
            else if (job.status === "failed" && index === Math.max(activeIndex, 0)) cls = "bg-hud-red";
            else if (index < activeIndex) cls = "bg-hud-green";
            else if (index === activeIndex && ["processing", "pending"].includes(job.status)) cls = "bg-hud-cyan blink";
            else if (job.status === "completed") cls = "bg-hud-green";
            return `<span class="w-2 h-2 ${cls}"></span>`;
        }).join("");
    }

    function closeJobsStream() {
        state.jobsStreamActive = false;
        if (state.jobsSocketReconnectTimer) {
            window.clearTimeout(state.jobsSocketReconnectTimer);
            state.jobsSocketReconnectTimer = null;
        }
        if (state.jobsPollTimer) {
            window.clearInterval(state.jobsPollTimer);
            state.jobsPollTimer = null;
        }
        if (state.jobsSocket) {
            state.jobsSocket.close();
            state.jobsSocket = null;
        }
    }

    function jobsPageIsActive(section) {
        return Boolean(section && document.body.contains(section) && section.classList.contains("active"));
    }

    function jobsSignature(jobsPayload, stats) {
        const jobs = (jobsPayload.jobs || []).map((job) => ({
            job_id: job.job_id,
            status: job.status,
            current_step: job.current_step,
            progress_percent: job.progress_percent,
            woo_link: job.woo_link,
            error: job.error,
        }));
        return JSON.stringify({
            jobs,
            total_processed: stats.total_processed || 0,
            avg_processing_time_sec: stats.avg_processing_time_sec || 0,
            avg_qa_score: stats.avg_qa_score || 0,
            avg_cost_per_article_usd: stats.avg_cost_per_article_usd || 0,
            dlq_size: stats.dlq_size || 0,
            status_filter: normalizeJobsStatusFilter(state.jobsStatusFilter),
            sort_key: state.jobsSortKey,
            sort_dir: state.jobsSortDir,
        });
    }

    function buildJobsShell() {
        return `
            <div class="max-w-7xl mx-auto">
                <div id="jobs-metrics" class="grid grid-cols-5 gap-4 mb-6"></div>

                <div class="hud-card overflow-hidden fade-in">
                    <span class="c-tl"></span><span class="c-br"></span>
                    <div class="header-strip px-6 py-3 flex items-center justify-between">
                        <div class="flex items-center gap-2">
                            <i class="fa-solid fa-list-check text-hud-cyan"></i>
                            <h3 class="font-display font-black text-xs text-white uppercase-widest">JOB PIPELINE</h3>
                            <span class="badge cyan">LIVE</span>
                        </div>
                        <span id="jobs-loaded-count" class="text-[10px] text-hud-muted uppercase-wide">0 jobs loaded</span>
                    </div>
                    <div class="px-6 py-3 border-b border-hud-cyan/10 flex justify-end">
                        <button id="jobs-refresh-btn" class="btn-ghost px-4 py-2.5 text-xs uppercase-wide font-bold flex items-center gap-2">
                            <i class="fa-solid fa-arrows-rotate"></i> REFRESH
                        </button>
                    </div>
                    <table class="hud-table">
                        <thead>
                            <tr>
                                <th class="w-[160px]"><button class="jobs-sort-header hover:text-hud-cyan" data-sort-key="job_id">JOB ID <span class="jobs-sort-indicator" data-sort-indicator="job_id"></span></button></th>
                                <th><button class="jobs-sort-header hover:text-hud-cyan" data-sort-key="title">URL / TITLE <span class="jobs-sort-indicator" data-sort-indicator="title"></span></button></th>
                                <th class="w-[320px]"><button class="jobs-sort-header hover:text-hud-cyan" data-sort-key="step">STEP PROGRESS <span class="jobs-sort-indicator" data-sort-indicator="step"></span></button></th>
                                <th class="w-[120px]"><button class="jobs-sort-header hover:text-hud-cyan" data-sort-key="status">STATUS <span class="jobs-sort-indicator" data-sort-indicator="status"></span></button></th>
                                <th class="w-[90px]"></th>
                            </tr>
                        </thead>
                        <tbody id="jobs-table-body"></tbody>
                    </table>
                </div>
            </div>
        `;
    }

    function jobsMetricCards(metrics) {
        return [
            { key: "processing", label: "PROCESSING", value: metrics.processing, tone: "cyan", statusFilter: "processing" },
            { key: "queued", label: "QUEUED", value: metrics.queued, tone: "amber", statusFilter: "pending" },
            { key: "completed", label: "COMPLETED", value: metrics.completed, tone: "green", statusFilter: "completed" },
            { key: "failed", label: "FAILED", value: metrics.failed, tone: "red", statusFilter: "failed" },
            { key: "avg-time", label: "AVG TIME", value: `${formatNumber(metrics.avgTime, 2)}s`, tone: "white", statusFilter: "" },
        ];
    }

    function jobsMetricCardClasses(card, active, includeFade) {
        const toneClass = card.tone === "amber" ? "amber" : card.tone === "green" ? "green" : card.tone === "red" ? "danger" : "";
        const clickable = card.statusFilter ? "cursor-pointer hover:border-hud-cyan/60 transition-colors jobs-status-filter" : "";
        const activeClass = active ? "ring-1 ring-hud-cyan/70 shadow-[0_0_24px_rgba(34,211,238,0.18)]" : "";
        const fadeClass = includeFade ? "fade-in" : "";
        return `hud-card ${toneClass} ${clickable} ${activeClass} p-4 ${fadeClass}`.replace(/\s+/g, " ").trim();
    }

    function jobsMetricLabelClass(tone) {
        return `text-[9px] ${tone === "green" ? "text-hud-green" : tone === "amber" ? "text-hud-amber" : tone === "red" ? "text-hud-red" : "text-hud-cyan"} uppercase-widest mb-1`;
    }

    function jobsMetricValueClass(tone) {
        return `metric-num text-2xl ${tone === "green" ? "text-hud-green" : tone === "amber" ? "text-hud-amber" : tone === "red" ? "text-hud-red" : "text-white"}`;
    }

    function renderJobsMetricCard(card, includeFade = true) {
        const active = card.statusFilter && normalizeJobsStatusFilter(state.jobsStatusFilter) === card.statusFilter;
        return `
            <div class="${jobsMetricCardClasses(card, active, includeFade)}" data-metric-key="${card.key}" ${card.statusFilter ? `data-status-filter="${card.statusFilter}" title="Click để lọc/bỏ lọc"` : ""}>
                <span class="c-tl"></span><span class="c-br"></span>
                <div class="jobs-metric-label ${jobsMetricLabelClass(card.tone)}">${card.label}${active ? " · FILTER" : ""}</div>
                <div class="jobs-metric-value ${jobsMetricValueClass(card.tone)}">${card.value}</div>
            </div>
        `;
    }

    function updateJobsMetrics(metricsEl, metricCards) {
        const existingCards = Array.from(metricsEl.querySelectorAll("[data-metric-key]"));
        const keys = metricCards.map((card) => card.key).join("|");
        const existingKeys = existingCards.map((card) => card.dataset.metricKey || "").join("|");
        if (keys !== existingKeys) {
            metricsEl.innerHTML = metricCards.map((card) => renderJobsMetricCard(card, true)).join("");
            return;
        }
        for (const card of metricCards) {
            const cardEl = metricsEl.querySelector(`[data-metric-key="${card.key}"]`);
            if (!cardEl) continue;
            const active = card.statusFilter && normalizeJobsStatusFilter(state.jobsStatusFilter) === card.statusFilter;
            cardEl.className = jobsMetricCardClasses(card, active, false);
            if (card.statusFilter) {
                cardEl.dataset.statusFilter = card.statusFilter;
                cardEl.title = "Click để lọc/bỏ lọc";
            } else {
                delete cardEl.dataset.statusFilter;
                cardEl.removeAttribute("title");
            }
            const labelEl = cardEl.querySelector(".jobs-metric-label");
            const valueEl = cardEl.querySelector(".jobs-metric-value");
            if (labelEl) {
                labelEl.className = `jobs-metric-label ${jobsMetricLabelClass(card.tone)}`;
                labelEl.textContent = `${card.label}${active ? " · FILTER" : ""}`;
            }
            if (valueEl) {
                valueEl.className = `jobs-metric-value ${jobsMetricValueClass(card.tone)}`;
                valueEl.textContent = String(card.value);
            }
        }
    }

    function renderJobsMarkup(jobsPayload, stats) {
        const jobs = jobsPayload.jobs || [];
        const metrics = jobsMetrics(jobs, stats);
        const visibleJobs = sortJobs(filterJobsByStatus(jobs));
        const groupedRows = [];
        const consumed = new Set();
        const childrenByParent = new Map();
        for (const job of visibleJobs) {
            if (!job.parent_job_id) continue;
            const list = childrenByParent.get(job.parent_job_id) || [];
            list.push(job);
            childrenByParent.set(job.parent_job_id, list);
        }
        for (const job of visibleJobs) {
            if (consumed.has(job.job_id)) continue;
            if (job.workflow_role === "shared_master") {
                groupedRows.push({ job, depth: 0, groupLabel: job.batch_id ? `BATCH ${job.batch_id.slice(0, 8)}` : "SHARED BATCH" });
                consumed.add(job.job_id);
                for (const child of childrenByParent.get(job.job_id) || []) {
                    groupedRows.push({ job: child, depth: 1, groupLabel: "" });
                    consumed.add(child.job_id);
                }
                continue;
            }
            if (job.parent_job_id && childrenByParent.has(job.parent_job_id)) {
                if (!consumed.has(job.job_id)) {
                    groupedRows.push({ job, depth: 1, groupLabel: "" });
                    consumed.add(job.job_id);
                }
                continue;
            }
            groupedRows.push({ job, depth: 0, groupLabel: job.batch_id ? `BATCH ${job.batch_id.slice(0, 8)}` : "" });
            consumed.add(job.job_id);
        }
        const metricCards = jobsMetricCards(metrics);
        const rowsHtml = groupedRows.map(({ job, depth, groupLabel }) => {
            const [tone, label] = badge(job.status);
            const siteBadge = job.site_name ? `<span class="badge cyan">${escapeHtml(job.site_name)}</span>` : "";
            const modeBadge = job.content_mode ? `<span class="badge ${job.content_mode === "per-site" ? "amber" : "green"}">${escapeHtml(job.content_mode)}</span>` : "";
            const roleBadge = job.workflow_role && job.workflow_role !== "standard" ? `<span class="badge purple">${escapeHtml(job.workflow_role)}</span>` : "";
            const actionHtml = job.status === "completed" && job.woo_link
                ? `<a href="${escapeHtml(job.woo_link)}" target="_blank" rel="noreferrer" class="text-hud-green hover:text-white text-xs" title="Open article"><i class="fa-solid fa-arrow-up-right-from-square"></i></a>`
                : job.status === "failed"
                    ? `<button class="text-hud-amber hover:text-white text-[10px] uppercase-wide jobs-rewrite-btn" data-job-id="${escapeHtml(job.job_id)}" title="Viết lại"><i class="fa-solid fa-arrows-rotate"></i></button>`
                    : `<button class="text-hud-cyan hover:text-white text-xs job-open-link" data-job-id="${escapeHtml(job.job_id)}" title="View detail"><i class="fa-solid fa-eye"></i></button>`;
            return `
                <tr>
                    <td class="font-mono ${tone === "red" ? "text-hud-red/80" : tone === "green" ? "text-hud-green/80" : "text-hud-cyan"}">${escapeHtml(job.job_id)}</td>
                    <td>
                        ${groupLabel ? `<div class="text-[9px] text-hud-cyan/70 uppercase-wide mb-1">${escapeHtml(groupLabel)}</div>` : ""}
                        <div class="text-white font-bold truncate max-w-md">${escapeHtml(truncate(job.title || job.url || job.job_id, 64))}</div>
                        <div class="text-[10px] text-hud-muted truncate max-w-md">${escapeHtml(job.url || "-")}</div>
                        <div class="mt-1 flex items-center gap-2 text-[9px] uppercase-wide ${depth ? "pl-4" : ""}">
                            ${depth ? `<span class="text-hud-cyan/60">└</span>` : ""}
                            ${siteBadge}
                            ${modeBadge}
                            ${roleBadge}
                        </div>
                    </td>
                    <td>
                        <div class="flex items-center gap-0.5">
                            ${progressDots(job)}
                            <span class="ml-2 text-[10px] ${job.status === "completed" ? "text-hud-green" : job.status === "failed" ? "text-hud-red" : "text-hud-cyan"} font-bold">${escapeHtml(stepLabel(job.current_step || job.status))}</span>
                            <span class="text-[10px] text-hud-muted">${escapeHtml(String(job.progress_percent || 0))}%</span>
                        </div>
                    </td>
                    <td><span class="badge ${tone}">${escapeHtml(label)}</span></td>
                    <td>${actionHtml}</td>
                </tr>
            `;
        }).join("") || `<tr><td colspan="5" class="text-hud-muted text-center py-6">No jobs found.</td></tr>`;
        return { metricCards, rowsHtml, jobsCount: visibleJobs.length };
    }

    function applyJobsMarkup(section, jobsPayload, stats) {
        const { metricCards, rowsHtml, jobsCount } = renderJobsMarkup(jobsPayload, stats);
        const metricsEl = section.querySelector("#jobs-metrics");
        const rowsEl = section.querySelector("#jobs-table-body");
        const countEl = section.querySelector("#jobs-loaded-count");
        if (metricsEl) updateJobsMetrics(metricsEl, metricCards);
        if (rowsEl) rowsEl.innerHTML = rowsHtml;
        if (countEl) countEl.textContent = `${jobsCount} jobs loaded`;
        section.querySelectorAll(".jobs-sort-indicator").forEach((indicator) => {
            const key = indicator.dataset.sortIndicator || "";
            indicator.textContent = state.jobsSortKey === key ? (state.jobsSortDir === "desc" ? "↓" : "↑") : "";
        });
    }

    function bindJobsActions(section) {
        bindJobOpenLinks(section);
        const refreshButton = section.querySelector("#jobs-refresh-btn");
        if (refreshButton && !refreshButton.dataset.bound) {
            refreshButton.dataset.bound = "1";
            refreshButton.addEventListener("click", renderJobsPage);
        }
        section.querySelectorAll(".jobs-rewrite-btn").forEach((button) => {
            button.addEventListener("click", async () => {
                try {
                    await fetchJSON(`/dlq/${encodeURIComponent(button.dataset.jobId)}/retry`, { method: "POST" });
                    await renderJobsPage();
                } catch (error) {
                    alert(`Retry failed: ${error.message}`);
                }
            });
        });
        section.querySelectorAll(".jobs-status-filter").forEach((card) => {
            if (card.dataset.bound) return;
            card.dataset.bound = "1";
            card.addEventListener("click", () => {
                const nextFilter = normalizeJobsStatusFilter(card.dataset.statusFilter);
                state.jobsStatusFilter = normalizeJobsStatusFilter(state.jobsStatusFilter) === nextFilter ? "" : nextFilter;
                state.jobsSignature = "";
                refreshJobsSnapshot(section).catch(() => renderJobsPage());
            });
        });
        section.querySelectorAll(".jobs-sort-header").forEach((button) => {
            if (button.dataset.bound) return;
            button.dataset.bound = "1";
            button.addEventListener("click", () => {
                const key = button.dataset.sortKey || "";
                if (!key) return;
                if (state.jobsSortKey === key) {
                    state.jobsSortDir = state.jobsSortDir === "asc" ? "desc" : "asc";
                } else {
                    state.jobsSortKey = key;
                    state.jobsSortDir = "asc";
                }
                state.jobsSignature = "";
                refreshJobsSnapshot(section).catch(() => renderJobsPage());
            });
        });
    }

    async function refreshJobsSnapshot(section) {
        if (!jobsPageIsActive(section)) return;
        const [jobsPayload, stats] = await Promise.all([
            fetchJSON("/jobs?limit=50"),
            fetchJSON("/stats"),
        ]);
        if (!jobsPageIsActive(section)) return;
        const nextSignature = jobsSignature(jobsPayload, stats);
        if (nextSignature === state.jobsSignature) return;
        state.jobsSignature = nextSignature;
        applyJobsMarkup(section, jobsPayload, stats);
        bindJobsActions(section);
    }

    function scheduleJobsReconnect(section) {
        if (!state.jobsStreamActive || !jobsPageIsActive(section) || state.jobsSocketReconnectTimer) return;
        const delay = Math.min(10000, 1000 * 2 ** state.jobsReconnectAttempts);
        state.jobsReconnectAttempts += 1;
        state.jobsSocketReconnectTimer = window.setTimeout(() => {
            state.jobsSocketReconnectTimer = null;
            if (state.jobsStreamActive && jobsPageIsActive(section)) openJobsStream(section);
        }, delay);
    }

    function openJobsStream(section) {
        closeJobsStream();
        state.jobsStreamActive = true;
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        state.jobsSocket = new WebSocket(`${protocol}//${window.location.host}${API_BASE}/realtime/ws`);
        state.jobsSocket.onopen = () => {
            state.jobsReconnectAttempts = 0;
            state.jobsSocket.send(JSON.stringify({ type: "subscribe", channels: ["jobs"], limit: 50 }));
        };
        state.jobsSocket.onmessage = (event) => {
            const payload = JSON.parse(event.data);
            if (payload.type !== "jobs.snapshot") {
                return;
            }
            const nextSignature = jobsSignature({ jobs: payload.jobs || [] }, payload.stats || {});
            if (nextSignature === state.jobsSignature) {
                return;
            }
            state.jobsSignature = nextSignature;
            applyJobsMarkup(section, { jobs: payload.jobs || [] }, payload.stats || {});
            bindJobsActions(section);
        };
        state.jobsSocket.onerror = () => {
            if (state.jobsSocket) state.jobsSocket.close();
        };
        state.jobsSocket.onclose = () => {
            state.jobsSocket = null;
            scheduleJobsReconnect(section);
        };
    }

    function closeDetailStream() {
        state.detailStreamActive = false;
        if (state.detailSocketReconnectTimer) {
            window.clearTimeout(state.detailSocketReconnectTimer);
            state.detailSocketReconnectTimer = null;
        }
        if (state.detailSocket) {
            state.detailSocket.close();
            state.detailSocket = null;
        }
    }

    function detailPageIsActive(section) {
        return Boolean(section && document.body.contains(section) && section.classList.contains("active"));
    }

    function detailSignature(detail) {
        return JSON.stringify({
            job_id: detail.job_id || state.selectedJobId,
            status: detail.status || "",
            current_step: detail.current_step || "",
            error: detail.error || "",
            woo_post_id: detail.woo_post_id || "",
            woo_link: detail.woo_link || "",
            updated_at: detail.updated_at || "",
            qa_score: (detail.qa_result || {}).overall_score || "",
        });
    }

    function scheduleDetailReconnect(section, jobId) {
        if (!state.detailStreamActive || !detailPageIsActive(section) || state.detailSocketReconnectTimer) return;
        const delay = Math.min(10000, 1000 * 2 ** state.detailReconnectAttempts);
        state.detailReconnectAttempts += 1;
        state.detailSocketReconnectTimer = window.setTimeout(() => {
            state.detailSocketReconnectTimer = null;
            if (state.detailStreamActive && detailPageIsActive(section) && state.selectedJobId === jobId) openDetailStream(section, jobId);
        }, delay);
    }

    function openDetailStream(section, jobId) {
        closeDetailStream();
        if (!jobId) return;
        state.detailStreamActive = true;
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        state.detailSocket = new WebSocket(`${protocol}//${window.location.host}${API_BASE}/realtime/ws`);
        state.detailSocket.onopen = () => {
            state.detailReconnectAttempts = 0;
            state.detailSocket.send(JSON.stringify({ type: "subscribe", channels: [`job:${jobId}`] }));
        };
        state.detailSocket.onmessage = (event) => {
            const payload = JSON.parse(event.data);
            if (payload.type !== "job.snapshot" || payload.job_id !== jobId || !payload.job) {
                return;
            }
            const nextSignature = detailSignature(payload.job);
            if (nextSignature === state.detailSignature) {
                return;
            }
            renderDetailSnapshot(section, payload.job, false);
        };
        state.detailSocket.onerror = () => {
            if (state.detailSocket) state.detailSocket.close();
        };
        state.detailSocket.onclose = () => {
            state.detailSocket = null;
            scheduleDetailReconnect(section, jobId);
        };
    }

    function renderDetailSnapshot(section, detail, includeFade = true) {
        const plan = detail.plan || {};
        const metrics = detail.metrics || {};
        const qa = detail.qa_result || {};
        const [tone, label] = badge(detail.status);
        const stepTimings = detail.step_timings || {};
        const stepHtml = PAGE_STEPS.map((step, index) => {
            const timings = stepTimings[step] || [];
            const latest = timings[timings.length - 1] || {};
            let cls = "";
            if (detail.status === "completed" || PAGE_STEPS.indexOf(detail.current_step) > index) cls = "done";
            if (detail.current_step === step && detail.status === "processing") cls = "active";
            if (detail.status === "failed" && detail.current_step === step) cls = "failed";
            const labelText = latest.duration_sec ? `${latest.duration_sec}s` : latest.status ? latest.status : "pending";
            return `
                    <div class="flex flex-col items-center gap-2 min-w-[90px]">
                        <div class="step-node ${cls}">${String(index + 1).padStart(2, "0")}</div>
                        <div class="text-[9px] ${cls === "done" ? "text-hud-green" : cls === "active" ? "text-hud-cyan" : cls === "failed" ? "text-hud-red" : "text-hud-muted"} font-bold uppercase-wide text-center">${escapeHtml(stepLabel(step))}</div>
                        <div class="text-[8px] text-hud-muted">${escapeHtml(labelText)}</div>
                    </div>
                    ${index < PAGE_STEPS.length - 1 ? `<div class="step-connector ${cls === "done" ? "done" : cls === "active" ? "active" : ""}"></div>` : ""}
                `;
        }).join("");
        section.innerHTML = `
                <div class="max-w-7xl mx-auto">
                    <div class="hud-card p-5 mb-6 ${includeFade ? "fade-in" : ""}">
                        <span class="c-tl"></span><span class="c-br"></span>
                        <div class="flex items-start justify-between gap-4">
                            <div class="flex-1">
                                <div class="flex items-center gap-3 mb-2">
                                    <span class="font-mono text-hud-cyan text-lg font-bold">${escapeHtml(detail.job_id || state.selectedJobId)}</span>
                                    <span class="badge ${tone}">${escapeHtml(label)}</span>
                                    <span class="badge ${detail.priority === "high" ? "amber" : "cyan"}">${escapeHtml(String(detail.priority || "normal").toUpperCase())}</span>
                                </div>
                                <h3 class="font-display font-black text-xl text-white uppercase-wide mb-1">${escapeHtml(plan.title || detail.fetch_result?.title || detail.url || "JOB DETAIL")}</h3>
                                <div class="flex items-center gap-2 text-[11px] text-hud-muted">
                                    <i class="fa-solid fa-link"></i>
                                    <a href="${escapeHtml(detail.url || "#")}" target="_blank" rel="noreferrer" class="text-hud-cyan hover:text-white transition">${escapeHtml(detail.url || "-")}</a>
                                </div>
                            </div>
                            <div class="text-right space-y-2">
                                <div><div class="text-[9px] text-hud-muted uppercase-widest">PROCESSING</div><div class="metric-num text-lg text-hud-cyan">${formatNumber(metrics.processing_time_sec || 0, 2)}s</div></div>
                                <div><div class="text-[9px] text-hud-muted uppercase-widest">COST</div><div class="metric-num text-lg text-hud-amber">${formatMoney(metrics.estimated_cost_usd || 0)}</div></div>
                            </div>
                        </div>
                    </div>
                    <div class="hud-card p-6 mb-6 ${includeFade ? "fade-in" : ""}">
                        <span class="c-tl"></span><span class="c-br"></span>
                        <div class="flex items-center justify-between mb-5">
                            <div class="flex items-center gap-2">
                                <i class="fa-solid fa-diagram-project text-hud-cyan"></i>
                                <h4 class="font-display font-black text-xs text-white uppercase-widest">PIPELINE PROGRESSION</h4>
                            </div>
                            <span class="text-[10px] text-hud-muted uppercase-wide">${escapeHtml(detail.current_step || detail.status || "-")}</span>
                        </div>
                        <div class="flex items-center justify-between overflow-x-auto pb-2">${stepHtml}</div>
                    </div>
                    <div class="grid grid-cols-3 gap-6">
                        <div class="col-span-2 space-y-4">
                            <div class="hud-card p-5"><span class="c-tl"></span><span class="c-br"></span><div class="text-[10px] font-bold text-hud-cyan uppercase-widest mb-3">PLAN</div><pre class="text-[11px] text-white/90 whitespace-pre-wrap">${escapeHtml(JSON.stringify(plan, null, 2))}</pre></div>
                            <div class="hud-card p-5"><span class="c-tl"></span><span class="c-br"></span><div class="text-[10px] font-bold text-hud-cyan uppercase-widest mb-3">KNOWLEDGE FACTS</div><pre class="text-[11px] text-white/90 whitespace-pre-wrap">${escapeHtml(JSON.stringify(detail.knowledge_facts || [], null, 2))}</pre></div>
                            <div class="hud-card p-5"><span class="c-tl"></span><span class="c-br"></span><div class="text-[10px] font-bold text-hud-cyan uppercase-widest mb-3">QA RESULT</div><pre class="text-[11px] text-white/90 whitespace-pre-wrap">${escapeHtml(JSON.stringify(qa, null, 2))}</pre></div>
                        </div>
                        <div class="space-y-4">
                            <div class="hud-card p-4"><span class="c-tl"></span><span class="c-br"></span><div class="text-[10px] font-bold text-hud-cyan uppercase-widest mb-3">OUTPUT</div><div class="space-y-2 text-[11px]"><div class="flex justify-between"><span class="text-hud-muted">Woo Post</span><span class="text-white">${escapeHtml(String(detail.woo_post_id || "-"))}</span></div><div class="flex justify-between"><span class="text-hud-muted">QA Score</span><span class="text-white">${escapeHtml(String(qa.overall_score || "-"))}</span></div><div class="flex justify-between"><span class="text-hud-muted">Tokens</span><span class="text-white">${escapeHtml(String(metrics.total_tokens_used || "-"))}</span></div></div>${detail.woo_link ? `<a class="mt-4 inline-block text-hud-cyan text-xs" href="${escapeHtml(detail.woo_link)}" target="_blank" rel="noreferrer">Open published link →</a>` : ""}</div>
                            <div class="hud-card p-4"><span class="c-tl"></span><span class="c-br"></span><div class="text-[10px] font-bold text-hud-cyan uppercase-widest mb-3">ERROR</div><div class="text-[11px] ${detail.error ? "text-hud-red" : "text-hud-muted"}">${escapeHtml(detail.error || "No error")}</div></div>
                        </div>
                    </div>
                </div>
            `;
        state.detailSignature = detailSignature(detail);
    }

    async function renderJobsPage() {
        const section = document.getElementById("page-jobs");
        if (!section) return;
        section.innerHTML = `<div class="max-w-7xl mx-auto text-hud-muted text-sm">Loading jobs...</div>`;
        try {
            const [jobsPayload, stats] = await Promise.all([
                fetchJSON("/jobs?limit=50"),
                fetchJSON("/stats"),
            ]);
            section.innerHTML = buildJobsShell();
            state.jobsSignature = jobsSignature(jobsPayload, stats);
            applyJobsMarkup(section, jobsPayload, stats);
            bindJobsActions(section);
            openJobsStream(section);
        } catch (error) {
            section.innerHTML = `<div class="max-w-7xl mx-auto text-hud-red text-sm">Failed to load jobs: ${escapeHtml(error.message)}</div>`;
        }
    }

    async function renderDetailPage() {
        const section = document.getElementById("page-detail");
        if (!section) return;
        if (!state.selectedJobId) {
            closeDetailStream();
            section.innerHTML = `<div class="max-w-5xl mx-auto hud-card p-6"><span class="c-tl"></span><span class="c-br"></span><div class="text-hud-muted text-sm">Chưa có job được chọn. Mở từ màn jobs hoặc submit một job mới.</div></div>`;
            return;
        }
        const jobId = state.selectedJobId;
        closeDetailStream();
        state.detailSignature = "";
        section.innerHTML = `<div class="max-w-7xl mx-auto text-hud-muted text-sm">Loading job detail...</div>`;
        try {
            const detail = await fetchJSON(`/job/${encodeURIComponent(jobId)}/detail`);
            renderDetailSnapshot(section, detail, true);
            openDetailStream(section, jobId);
        } catch (error) {
            section.innerHTML = `<div class="max-w-6xl mx-auto text-hud-red text-sm">Failed to load job detail: ${escapeHtml(error.message)}</div>`;
        }
    }

    async function renderDlqPage() {
        const section = document.getElementById("page-dlq");
        if (!section) return;
        section.innerHTML = `<div class="max-w-6xl mx-auto text-hud-muted text-sm">Loading DLQ...</div>`;
        try {
            const [dlq, stats] = await Promise.all([fetchJSON("/dlq"), fetchJSON("/stats")]);
            section.innerHTML = `
                <div class="max-w-6xl mx-auto">
                    <div class="hud-card danger p-4 mb-6 fade-in">
                        <span class="c-tl"></span><span class="c-br"></span>
                        <div class="flex items-center gap-4">
                            <div class="w-12 h-12 border-2 border-hud-red flex items-center justify-center flex-shrink-0" style="clip-path: polygon(50% 0, 100% 25%, 100% 75%, 50% 100%, 0 75%, 0 25%);">
                                <i class="fa-solid fa-triangle-exclamation text-hud-red text-lg blink"></i>
                            </div>
                            <div class="flex-1">
                                <h3 class="font-display font-bold text-sm text-white uppercase-wide mb-1"><span class="text-hud-red">${dlq.total} JOBS</span> AWAITING MANUAL REVIEW</h3>
                                <p class="text-[11px] text-hud-muted uppercase-wide">Failed after retry budget · Retry or force publish below</p>
                            </div>
                        </div>
                    </div>
                    <div class="grid grid-cols-4 gap-4 mb-6">
                        <div class="hud-card danger p-4"><span class="c-tl"></span><span class="c-br"></span><div class="text-[9px] text-hud-red uppercase-widest mb-1">IN DLQ</div><div class="metric-num text-2xl text-hud-red">${dlq.total}</div></div>
                        <div class="hud-card p-4"><span class="c-tl"></span><span class="c-br"></span><div class="text-[9px] text-hud-cyan uppercase-widest mb-1">TOTAL PROCESSED</div><div class="metric-num text-2xl text-white">${formatNumber(stats.total_processed)}</div></div>
                        <div class="hud-card green p-4"><span class="c-tl"></span><span class="c-br"></span><div class="text-[9px] text-hud-green uppercase-widest mb-1">SUCCESS RATE</div><div class="metric-num text-2xl text-hud-green">${formatNumber((stats.success_rate || 0) * 100, 1)}%</div></div>
                        <div class="hud-card amber p-4"><span class="c-tl"></span><span class="c-br"></span><div class="text-[9px] text-hud-amber uppercase-widest mb-1">AVG QA</div><div class="metric-num text-2xl text-hud-amber">${formatNumber(stats.avg_qa_score || 0, 2)}</div></div>
                    </div>
                    <div class="space-y-4">
                        ${(dlq.jobs || []).map((job) => `
                            <div class="hud-card danger fade-in">
                                <span class="c-tl"></span><span class="c-br"></span>
                                <div class="p-5">
                                    <div class="flex items-start gap-4">
                                        <div class="w-10 h-10 border border-hud-red flex items-center justify-center flex-shrink-0" style="clip-path: polygon(50% 0, 100% 25%, 100% 75%, 50% 100%, 0 75%, 0 25%);">
                                            <i class="fa-solid fa-xmark text-hud-red"></i>
                                        </div>
                                        <div class="flex-1">
                                            <div class="flex items-center gap-3 mb-1">
                                                <span class="font-mono text-hud-red font-bold">${escapeHtml(job.job_id)}</span>
                                                <span class="badge red">FAILED</span>
                                                <span class="text-[10px] text-hud-muted ml-auto">${escapeHtml(job.failed_at || "")}</span>
                                            </div>
                                            <h4 class="font-display font-bold text-sm text-white uppercase-wide mb-1">${escapeHtml(truncate(job.url, 84))}</h4>
                                            <div class="text-[11px] text-hud-muted">${escapeHtml(job.reason || "-")}</div>
                                        </div>
                                    </div>
                                    <div class="flex gap-2 mt-4 pt-4 border-t border-hud-red/20">
                                        <button class="btn-primary px-4 py-2 text-[10px] uppercase-wide font-bold dlq-retry-btn" data-job-id="${escapeHtml(job.job_id)}"><i class="fa-solid fa-arrows-rotate"></i> RETRY</button>
                                        <button class="btn-success px-4 py-2 text-[10px] uppercase-wide font-bold dlq-force-btn" data-job-id="${escapeHtml(job.job_id)}"><i class="fa-solid fa-bolt"></i> FORCE PUBLISH</button>
                                        <button class="btn-danger px-4 py-2 text-[10px] uppercase-wide font-bold ml-auto dlq-delete-btn" data-job-id="${escapeHtml(job.job_id)}"><i class="fa-solid fa-trash"></i> DELETE</button>
                                    </div>
                                </div>
                            </div>
                        `).join("") || `<div class="hud-card p-5"><span class="c-tl"></span><span class="c-br"></span><div class="text-hud-green text-sm">DLQ is empty.</div></div>`}
                    </div>
                </div>
            `;
            section.querySelectorAll(".dlq-retry-btn").forEach((button) => button.addEventListener("click", async () => {
                await fetchJSON(`/dlq/${encodeURIComponent(button.dataset.jobId)}/retry`, { method: "POST" });
                renderDlqPage();
            }));
            section.querySelectorAll(".dlq-force-btn").forEach((button) => button.addEventListener("click", async () => {
                await fetchJSON(`/dlq/${encodeURIComponent(button.dataset.jobId)}/publish-anyway`, { method: "POST" });
                renderDlqPage();
            }));
            section.querySelectorAll(".dlq-delete-btn").forEach((button) => button.addEventListener("click", async () => {
                await fetchJSON(`/dlq/${encodeURIComponent(button.dataset.jobId)}`, { method: "DELETE" });
                renderDlqPage();
            }));
        } catch (error) {
            section.innerHTML = `<div class="max-w-6xl mx-auto text-hud-red text-sm">Failed to load DLQ: ${escapeHtml(error.message)}</div>`;
        }
    }

    async function renderKnowledgePage() {
        const section = document.getElementById("page-knowledge");
        if (!section) return;
        section.innerHTML = `<div class="max-w-7xl mx-auto text-hud-muted text-sm">Loading knowledge base...</div>`;
        try {
            const [taxonomy, sources, categoriesPayload] = await Promise.all([
                fetchJSON("/rag/taxonomy"),
                fetchJSON("/rag/sources?limit=50"),
                fetchJSON("/rag/categories"),
            ]);
            const categories = categoriesPayload.categories || [];
            section.innerHTML = `
                <div class="max-w-7xl mx-auto">
                    <div class="grid grid-cols-4 gap-4 mb-6">
                        <div class="hud-card p-4"><span class="c-tl"></span><span class="c-br"></span><div class="text-[9px] text-hud-cyan uppercase-widest mb-1">PRIMARY CATEGORIES</div><div class="metric-num text-3xl text-white">${categories.length}</div></div>
                        <div class="hud-card green p-4"><span class="c-tl"></span><span class="c-br"></span><div class="text-[9px] text-hud-green uppercase-widest mb-1">SOURCES</div><div class="metric-num text-3xl text-hud-green">${sources.total}</div></div>
                        <div class="hud-card amber p-4"><span class="c-tl"></span><span class="c-br"></span><div class="text-[9px] text-hud-amber uppercase-widest mb-1">SUBCATEGORIES</div><div class="metric-num text-3xl text-hud-amber">${taxonomy.subcategories.length}</div></div>
                        <div class="hud-card p-4"><span class="c-tl"></span><span class="c-br"></span><div class="text-[9px] text-hud-cyan uppercase-widest mb-1">DOCUMENTS</div><div class="metric-num text-3xl text-white">${taxonomy.document_count}</div></div>
                    </div>
                    <div class="grid grid-cols-3 gap-6">
                        <div class="col-span-2 space-y-6">
                            <div class="hud-card fade-in">
                                <span class="c-tl"></span><span class="c-br"></span>
                                <div class="header-strip px-5 py-3 flex items-center gap-2">
                                    <i class="fa-solid fa-seedling text-hud-amber"></i>
                                    <span class="font-display font-black text-xs text-white uppercase-widest">INGEST URL</span>
                                    <button id="rag-open-category-dialog" class="ml-auto btn-ghost px-3 py-1.5 text-[10px] uppercase-wide font-bold flex items-center gap-2">
                                        <i class="fa-solid fa-folder-plus"></i> CREATE CATEGORY
                                    </button>
                                </div>
                                <div class="p-5 space-y-4">
                                    <div class="input-wrap"><textarea id="rag-ingest-urls" rows="6" placeholder="Mỗi dòng một URL&#10;https://example.com/source-1&#10;https://example.com/source-2" class="hud-textarea w-full px-4 py-3 text-sm font-mono"></textarea></div>
                                    <div class="grid grid-cols-[1fr_auto] gap-3 items-end">
                                        <div>
                                            <label class="text-[10px] font-bold text-hud-cyan uppercase-widest mb-2 block">Danh mục lớn</label>
                                            <div class="input-wrap">
                                                <select id="rag-ingest-category" class="hud-input w-full px-4 py-2.5 text-sm">
                                                    <option value="">Chọn danh mục</option>
                                                    ${categories.map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(item)}</option>`).join("")}
                                                </select>
                                            </div>
                                        </div>
                                        <div class="text-[10px] text-hud-muted pb-2">${categories.length ? `${categories.length} categories` : "Chưa có danh mục"}</div>
                                    </div>
                                    <button id="rag-ingest-btn" class="btn-primary w-full py-2.5 text-xs uppercase-wide font-bold tracking-widest flex items-center justify-center gap-2"><i class="fa-solid fa-plus"></i> INGEST SOURCE</button>
                                    <div id="rag-ingest-feedback" class="text-[11px] text-hud-muted"></div>
                                </div>
                            </div>
                            <div class="hud-card fade-in">
                                <span class="c-tl"></span><span class="c-br"></span>
                                <div class="header-strip px-5 py-3 flex items-center gap-2">
                                    <i class="fa-solid fa-magnifying-glass text-hud-cyan"></i>
                                    <span class="font-display font-black text-xs text-white uppercase-widest">SOURCE LIST</span>
                                </div>
                                <div class="p-5">
                                    <table class="hud-table">
                                        <thead><tr><th>URL</th><th class="w-[120px]">CATEGORY</th><th class="w-[110px]">DOCS</th><th class="w-[180px]">UPDATED</th><th class="w-[60px]"></th></tr></thead>
                                        <tbody>
                                            ${(sources.sources || []).map((source) => `
                                                <tr>
                                                    <td>
                                                        <div class="text-white font-bold">${escapeHtml(truncate(source.title || source.source_url, 58))}</div>
                                                        <div class="text-[10px] text-hud-cyan">${escapeHtml(source.source_url)}</div>
                                                    </td>
                                                    <td><span class="badge cyan">${escapeHtml(source.primary_category || "-")}</span></td>
                                                    <td class="font-mono text-hud-cyan">${escapeHtml(String(source.document_count || 0))}</td>
                                                    <td class="text-[10px] text-hud-muted">${escapeHtml(formatDate(source.last_ingested_at))}</td>
                                                    <td><button class="text-hud-red hover:text-white rag-source-delete" data-url="${escapeHtml(source.source_url)}"><i class="fa-solid fa-trash"></i></button></td>
                                                </tr>
                                            `).join("") || `<tr><td colspan="5" class="text-center text-hud-muted py-6">No RAG sources yet.</td></tr>`}
                                        </tbody>
                                    </table>
                                </div>
                            </div>
                        </div>
                        <div class="space-y-4">
                            <div class="hud-card p-4"><span class="c-tl"></span><span class="c-br"></span><div class="text-[10px] font-bold text-hud-cyan uppercase-widest mb-3">TAXONOMY</div><div class="space-y-2 text-[11px]"><div><span class="text-hud-muted">Primary:</span> <span class="text-white">${escapeHtml(taxonomy.primary_category || taxonomy.available_primary_categories.join(", ") || "-")}</span></div><div><span class="text-hud-muted">Knowledge types:</span> <span class="text-white">${escapeHtml((taxonomy.knowledge_types || []).join(", ") || "-")}</span></div><div><span class="text-hud-muted">Usage intents:</span> <span class="text-white">${escapeHtml((taxonomy.usage_intents || []).join(", ") || "-")}</span></div></div></div>
                            <div class="hud-card p-4"><span class="c-tl"></span><span class="c-br"></span><div class="text-[10px] font-bold text-hud-cyan uppercase-widest mb-3">SUBCATEGORIES</div><div class="flex flex-wrap gap-2">${(taxonomy.subcategories || []).map((item) => `<span class="badge amber">${escapeHtml(item)}</span>`).join("") || `<span class="text-hud-muted text-[11px]">No subcategories.</span>`}</div></div>
                            <div class="hud-card p-4"><span class="c-tl"></span><span class="c-br"></span><div class="text-[10px] font-bold text-hud-cyan uppercase-widest mb-3">CATEGORIES</div><div class="flex flex-wrap gap-2">${categories.map((item) => `<span class="badge cyan">${escapeHtml(item)}</span>`).join("") || `<span class="text-hud-muted text-[11px]">Chưa có danh mục.</span>`}</div></div>
                        </div>
                    </div>
                    <dialog id="rag-category-dialog" class="bg-[#07141c] border border-hud-cyan/30 text-white w-full max-w-md p-0 backdrop:bg-black/70">
                        <div class="header-strip px-5 py-3 flex items-center gap-2">
                            <i class="fa-solid fa-folder-plus text-hud-cyan"></i>
                            <span class="font-display font-black text-xs text-white uppercase-widest">CREATE CATEGORY</span>
                        </div>
                        <div class="p-5 space-y-4">
                            <div>
                                <label class="text-[10px] font-bold text-hud-cyan uppercase-widest mb-2 block">Tên danh mục</label>
                                <div class="input-wrap"><input id="rag-category-name" type="text" class="hud-input w-full px-4 py-2.5 text-sm" placeholder="vd: trà"/></div>
                            </div>
                            <div id="rag-category-feedback" class="text-[11px] text-hud-muted"></div>
                            <div class="flex gap-2">
                                <button id="rag-category-save" class="btn-primary flex-1 px-4 py-2.5 text-[10px] uppercase-wide font-bold"><i class="fa-solid fa-floppy-disk"></i> SAVE</button>
                                <button id="rag-category-cancel" class="btn-ghost px-4 py-2.5 text-[10px] uppercase-wide font-bold">CLOSE</button>
                            </div>
                        </div>
                    </dialog>
                </div>
            `;
            section.querySelector("#rag-ingest-btn")?.addEventListener("click", async () => {
                const urls = (section.querySelector("#rag-ingest-urls")?.value || "")
                    .split(/\r?\n/)
                    .map((item) => item.trim())
                    .filter(Boolean);
                const category = section.querySelector("#rag-ingest-category")?.value.trim();
                const feedback = section.querySelector("#rag-ingest-feedback");
                if (!urls.length || !category) {
                    if (feedback) feedback.textContent = "Cần nhập ít nhất một URL và phải chọn danh mục.";
                    return;
                }
                try {
                    if (feedback) feedback.textContent = "Đang ingest...";
                    let totalDocs = 0;
                    for (const url of urls) {
                        const result = await fetchJSON("/rag/ingest", {
                            method: "POST",
                            body: JSON.stringify({
                                url,
                                manual_categories: [category],
                                manual_tags: [],
                                note: null,
                                force_reingest: true,
                            }),
                        });
                        totalDocs += Number(result.documents_count || 0);
                    }
                    if (feedback) feedback.textContent = `Đã ingest ${urls.length} URL, tổng ${totalDocs} chunks.`;
                    renderKnowledgePage();
                } catch (error) {
                    if (feedback) feedback.textContent = `Ingest failed: ${error.message}`;
                }
            });
            const categoryDialog = section.querySelector("#rag-category-dialog");
            section.querySelector("#rag-open-category-dialog")?.addEventListener("click", () => {
                categoryDialog?.showModal();
            });
            section.querySelector("#rag-category-cancel")?.addEventListener("click", () => {
                categoryDialog?.close();
            });
            section.querySelector("#rag-category-save")?.addEventListener("click", async () => {
                const input = section.querySelector("#rag-category-name");
                const feedback = section.querySelector("#rag-category-feedback");
                const name = input?.value.trim() || "";
                if (!name) {
                    if (feedback) feedback.textContent = "Tên danh mục là bắt buộc.";
                    return;
                }
                try {
                    if (feedback) feedback.textContent = "Đang tạo danh mục...";
                    await fetchJSON("/rag/categories", {
                        method: "POST",
                        body: JSON.stringify({ name }),
                    });
                    categoryDialog?.close();
                    renderKnowledgePage();
                } catch (error) {
                    if (feedback) feedback.textContent = `Tạo danh mục thất bại: ${error.message}`;
                }
            });
            section.querySelectorAll(".rag-source-delete").forEach((button) => button.addEventListener("click", async () => {
                await fetchJSON(`/rag/source?url=${encodeURIComponent(button.dataset.url)}`, { method: "DELETE" });
                renderKnowledgePage();
            }));
        } catch (error) {
            section.innerHTML = `<div class="max-w-7xl mx-auto text-hud-red text-sm">Failed to load knowledge base: ${escapeHtml(error.message)}</div>`;
        }
    }

    function siteStatusBadge(status) {
        const key = String(status || "untested").toLowerCase();
        if (key === "connected") return ["green", "CONNECTED"];
        if (key === "unauthorized") return ["amber", "AUTH FAIL"];
        if (["error", "offline"].includes(key)) return ["red", key.toUpperCase()];
        return ["cyan", "UNTESTED"];
    }

    function getEmptySiteForm() {
        return {
            url: "",
            site_name: "",
            topic: "",
            primary_color: "#22c55e",
            consumer_key: "",
            consumer_secret: "",
            username: "",
            app_password: "",
        };
    }

    function normalizeSiteForForm(site) {
        if (!site) return getEmptySiteForm();
        return {
            url: site.url || "",
            site_name: site.site_name || "",
            topic: site.topic || "",
            primary_color: site.primary_color || "#22c55e",
            consumer_key: site.consumer_key || "",
            consumer_secret: site.consumer_secret || "",
            username: site.username || "",
            app_password: site.app_password || "",
        };
    }

    async function renderWebsiteManagePage() {
        const section = document.getElementById("page-website-manage");
        if (!section) return;
        section.innerHTML = `<div class="max-w-7xl mx-auto text-hud-muted text-sm">Loading sites...</div>`;
        try {
            const payload = await fetchJSON("/sites");
            const sites = payload.sites || [];
            if (!state.selectedSiteId && sites.length) {
                state.selectedSiteId = sites[0].site_id;
            }
            const selectedSite = sites.find((item) => item.site_id === state.selectedSiteId) || null;
            const form = normalizeSiteForForm(selectedSite);
            const connectedCount = sites.filter((item) => item.last_test_status === "connected").length;
            const issueCount = sites.filter((item) => ["offline", "error", "unauthorized"].includes(item.last_test_status)).length;
            const keyCount = sites.reduce((sum, item) => sum + (item.consumer_key ? 1 : 0) + (item.consumer_secret ? 1 : 0), 0);
            section.innerHTML = `
                <div class="max-w-7xl mx-auto">
                    <div class="grid grid-cols-4 gap-4 mb-6">
                        <div class="hud-card p-4"><span class="c-tl"></span><span class="c-br"></span><div class="text-[9px] text-hud-cyan uppercase-widest mb-1">TOTAL SITES</div><div class="metric-num text-2xl text-white">${sites.length}</div></div>
                        <div class="hud-card green p-4"><span class="c-tl"></span><span class="c-br"></span><div class="text-[9px] text-hud-green uppercase-widest mb-1">CONNECTED</div><div class="metric-num text-2xl text-hud-green">${connectedCount}</div></div>
                        <div class="hud-card amber p-4"><span class="c-tl"></span><span class="c-br"></span><div class="text-[9px] text-hud-amber uppercase-widest mb-1">API KEYS</div><div class="metric-num text-2xl text-hud-amber">${keyCount}</div></div>
                        <div class="hud-card danger p-4"><span class="c-tl"></span><span class="c-br"></span><div class="text-[9px] text-hud-red uppercase-widest mb-1">ISSUES</div><div class="metric-num text-2xl text-hud-red">${issueCount}</div></div>
                    </div>
                    <div class="grid grid-cols-[1.2fr_1fr] gap-6">
                        <div class="hud-card fade-in">
                            <span class="c-tl"></span><span class="c-br"></span>
                            <div class="header-strip px-5 py-3 flex items-center gap-2">
                                <i class="fa-solid fa-globe text-hud-cyan"></i>
                                <span class="font-display font-black text-xs text-white uppercase-widest">WEBSITES</span>
                                <button id="site-new-btn" class="ml-auto btn-primary px-4 py-2 text-[10px] uppercase-wide font-bold flex items-center gap-2">
                                    <i class="fa-solid fa-plus"></i> NEW SITE
                                </button>
                            </div>
                            <div class="p-5 space-y-3">
                                ${sites.map((site) => {
                                    const [tone, label] = siteStatusBadge(site.last_test_status);
                                    const isActive = site.site_id === state.selectedSiteId;
                                    return `
                                        <button class="w-full text-left border ${isActive ? "border-hud-cyan/50 bg-hud-cyan/10" : tone === "red" ? "border-hud-red/25 bg-black/30" : "border-hud-green/20 bg-black/30"} p-4 hover:border-hud-cyan transition site-select-btn" data-site-id="${escapeHtml(site.site_id)}">
                                            <div class="flex items-start justify-between gap-3 mb-2">
                                                <div>
                                                    <div class="font-display font-bold text-sm text-white uppercase-wide">${escapeHtml(site.site_name || site.url)}</div>
                                                    <div class="text-[10px] text-hud-cyan truncate">${escapeHtml(site.url)}</div>
                                                </div>
                                                <span class="badge ${tone}">${escapeHtml(label)}</span>
                                            </div>
                                            <div class="grid grid-cols-2 gap-3 text-[10px] text-hud-muted">
                                                <div>Chủ đề: <span class="text-white">${escapeHtml(site.topic || "-")}</span></div>
                                                <div>Màu: <span class="text-white">${escapeHtml(site.primary_color || "-")}</span></div>
                                                <div>Tài khoản: <span class="text-white">${escapeHtml(site.username || "-")}</span></div>
                                                <div>App password: <span class="text-white font-mono">${escapeHtml(maskSecret(site.app_password))}</span></div>
                                            </div>
                                            <div class="mt-3 flex flex-wrap gap-2">
                                                ${site.consumer_key || site.consumer_secret
                                                    ? `<span class="badge cyan">CONSUMER_KEY · ${escapeHtml(maskSecret(site.consumer_key))}</span>
                                                       <span class="badge cyan">CONSUMER_SECRET · ${escapeHtml(maskSecret(site.consumer_secret))}</span>`
                                                    : `<span class="text-[10px] text-hud-muted">No API keys</span>`}
                                            </div>
                                            <div class="mt-3 text-[10px] ${tone === "red" ? "text-hud-red" : tone === "amber" ? "text-hud-amber" : "text-hud-muted"}">${escapeHtml(site.last_test_message || "Chưa kiểm tra kết nối.")}</div>
                                        </button>
                                    `;
                                }).join("") || `<div class="text-hud-muted text-[11px]">Chưa có site nào.</div>`}
                            </div>
                        </div>
                        <div class="hud-card fade-in">
                            <span class="c-tl"></span><span class="c-br"></span>
                            <div class="header-strip px-5 py-3 flex items-center gap-2">
                                <i class="fa-solid fa-pen-to-square text-hud-amber"></i>
                                <span class="font-display font-black text-xs text-white uppercase-widest">${selectedSite ? "EDIT SITE" : "CREATE SITE"}</span>
                            </div>
                            <div class="p-5 space-y-4">
                                <div id="site-form-feedback" class="hidden text-[11px] border p-3"></div>
                                <div>
                                    <label class="text-[10px] font-bold text-hud-cyan uppercase-widest mb-2 block">URL</label>
                                    <div class="input-wrap"><input id="site-url" type="url" class="hud-input w-full px-4 py-2.5 text-sm" value="${escapeHtml(form.url)}" placeholder="https://example.com"/></div>
                                </div>
                                <div class="grid grid-cols-2 gap-3">
                                    <div>
                                        <label class="text-[10px] font-bold text-hud-cyan uppercase-widest mb-2 block">Tên website</label>
                                        <div class="input-wrap"><input id="site-name" type="text" class="hud-input w-full px-4 py-2.5 text-sm" value="${escapeHtml(form.site_name)}" placeholder="Lộc Tân Cương"/></div>
                                    </div>
                                    <div>
                                        <label class="text-[10px] font-bold text-hud-cyan uppercase-widest mb-2 block">Chủ đề</label>
                                        <div class="input-wrap"><input id="site-topic" type="text" class="hud-input w-full px-4 py-2.5 text-sm" value="${escapeHtml(form.topic)}" placeholder="Trà, thời trang..."/></div>
                                    </div>
                                </div>
                                <div>
                                    <label class="text-[10px] font-bold text-hud-cyan uppercase-widest mb-2 block">Màu chủ đạo</label>
                                    <div class="flex gap-3">
                                        <input id="site-color" type="color" value="${escapeHtml(form.primary_color)}" class="w-12 h-10 bg-transparent border border-hud-cyan/30"/>
                                        <div class="input-wrap flex-1"><input id="site-color-text" type="text" class="hud-input w-full px-4 py-2.5 text-sm font-mono" value="${escapeHtml(form.primary_color)}" placeholder="#22c55e"/></div>
                                    </div>
                                </div>
                                <div class="grid grid-cols-2 gap-3">
                                    <div>
                                        <label class="text-[10px] font-bold text-hud-cyan uppercase-widest mb-2 block">CONSUMER_KEY</label>
                                        <div class="input-wrap"><input id="site-consumer-key" type="text" class="hud-input w-full px-4 py-2.5 text-sm font-mono" value="${escapeHtml(form.consumer_key)}" placeholder="ck_..."/></div>
                                    </div>
                                    <div>
                                        <label class="text-[10px] font-bold text-hud-cyan uppercase-widest mb-2 block">CONSUMER_SECRET</label>
                                        <div class="input-wrap"><input id="site-consumer-secret" type="text" class="hud-input w-full px-4 py-2.5 text-sm font-mono" value="${escapeHtml(form.consumer_secret)}" placeholder="cs_..."/></div>
                                    </div>
                                </div>
                                <div class="grid grid-cols-2 gap-3">
                                    <div>
                                        <label class="text-[10px] font-bold text-hud-cyan uppercase-widest mb-2 block">Tài khoản</label>
                                        <div class="input-wrap"><input id="site-username" type="text" class="hud-input w-full px-4 py-2.5 text-sm" value="${escapeHtml(form.username)}" placeholder="admin"/></div>
                                    </div>
                                    <div>
                                        <label class="text-[10px] font-bold text-hud-cyan uppercase-widest mb-2 block">App password</label>
                                        <div class="input-wrap"><input id="site-app-password" type="text" class="hud-input w-full px-4 py-2.5 text-sm font-mono" value="${escapeHtml(form.app_password)}" placeholder="xxxx xxxx xxxx"/></div>
                                    </div>
                                </div>
                                <div class="flex gap-2 pt-2 border-t border-hud-cyan/10">
                                    <button id="site-save-btn" class="btn-primary flex-1 px-4 py-2.5 text-[10px] uppercase-wide font-bold"><i class="fa-solid fa-floppy-disk"></i> ${selectedSite ? "SAVE SITE" : "CREATE SITE"}</button>
                                    ${selectedSite ? `<button id="site-test-btn" class="btn-ghost px-4 py-2.5 text-[10px] uppercase-wide font-bold"><i class="fa-solid fa-flask"></i> TEST</button>` : ""}
                                    ${selectedSite ? `<button id="site-delete-btn" class="btn-danger px-4 py-2.5 text-[10px] uppercase-wide font-bold"><i class="fa-solid fa-trash"></i> DELETE</button>` : ""}
                                </div>
                                ${selectedSite ? `
                                    <div class="text-[10px] text-hud-muted border-t border-hud-cyan/10 pt-3">
                                        <div>Lần test gần nhất: <span class="text-white">${escapeHtml(selectedSite.last_tested_at ? formatDate(selectedSite.last_tested_at) : "-")}</span></div>
                                        <div>Trạng thái: <span class="${siteStatusBadge(selectedSite.last_test_status)[0] === "green" ? "text-hud-green" : siteStatusBadge(selectedSite.last_test_status)[0] === "amber" ? "text-hud-amber" : siteStatusBadge(selectedSite.last_test_status)[0] === "red" ? "text-hud-red" : "text-white"}">${escapeHtml(selectedSite.last_test_status || "untested")}</span></div>
                                    </div>
                                ` : ""}
                            </div>
                        </div>
                    </div>
                </div>
            `;

            const colorInput = section.querySelector("#site-color");
            const colorText = section.querySelector("#site-color-text");
            colorInput?.addEventListener("input", () => {
                if (colorText) colorText.value = colorInput.value;
            });
            colorText?.addEventListener("input", () => {
                if (colorInput && /^#[0-9a-fA-F]{6}$/.test(colorText.value.trim())) {
                    colorInput.value = colorText.value.trim();
                }
            });
            section.querySelectorAll(".site-select-btn").forEach((button) => button.addEventListener("click", () => {
                state.selectedSiteId = button.dataset.siteId || "";
                renderWebsiteManagePage();
            }));
            section.querySelector("#site-new-btn")?.addEventListener("click", () => {
                state.selectedSiteId = "";
                renderWebsiteManagePage();
            });
            section.querySelector("#site-save-btn")?.addEventListener("click", async () => {
                const feedback = section.querySelector("#site-form-feedback");
                const requestBody = {
                    url: section.querySelector("#site-url")?.value.trim() || "",
                    site_name: section.querySelector("#site-name")?.value.trim() || "",
                    topic: section.querySelector("#site-topic")?.value.trim() || "",
                    primary_color: section.querySelector("#site-color-text")?.value.trim() || "#22c55e",
                    consumer_key: section.querySelector("#site-consumer-key")?.value.trim() || "",
                    consumer_secret: section.querySelector("#site-consumer-secret")?.value.trim() || "",
                    username: section.querySelector("#site-username")?.value.trim() || "",
                    app_password: section.querySelector("#site-app-password")?.value.trim() || "",
                };
                try {
                    if (feedback) {
                        feedback.className = "text-[11px] border p-3 text-hud-cyan border-hud-cyan/30 bg-hud-cyan/10";
                        feedback.textContent = "Đang lưu...";
                        feedback.classList.remove("hidden");
                    }
                    const saved = selectedSite
                        ? await fetchJSON(`/sites/${encodeURIComponent(selectedSite.site_id)}`, { method: "PUT", body: JSON.stringify(requestBody) })
                        : await fetchJSON("/sites", { method: "POST", body: JSON.stringify(requestBody) });
                    state.selectedSiteId = saved.site_id;
                    await renderWebsiteManagePage();
                } catch (error) {
                    if (feedback) {
                        feedback.className = "text-[11px] border p-3 text-hud-red border-hud-red/30 bg-hud-red/10";
                        feedback.textContent = `Lưu thất bại: ${error.message}`;
                        feedback.classList.remove("hidden");
                    }
                }
            });
            section.querySelector("#site-test-btn")?.addEventListener("click", async () => {
                const feedback = section.querySelector("#site-form-feedback");
                if (!selectedSite) return;
                try {
                    if (feedback) {
                        feedback.className = "text-[11px] border p-3 text-hud-cyan border-hud-cyan/30 bg-hud-cyan/10";
                        feedback.textContent = "Đang kiểm tra kết nối...";
                        feedback.classList.remove("hidden");
                    }
                    const result = await fetchJSON(`/sites/${encodeURIComponent(selectedSite.site_id)}/test`, { method: "POST" });
                    if (feedback) {
                        const ok = result.status === "connected";
                        feedback.className = `text-[11px] border p-3 ${ok ? "text-hud-green border-hud-green/30 bg-hud-green/10" : "text-hud-amber border-hud-amber/30 bg-hud-amber/10"}`;
                        feedback.textContent = result.message;
                        feedback.classList.remove("hidden");
                    }
                    await renderWebsiteManagePage();
                } catch (error) {
                    if (feedback) {
                        feedback.className = "text-[11px] border p-3 text-hud-red border-hud-red/30 bg-hud-red/10";
                        feedback.textContent = `Test thất bại: ${error.message}`;
                        feedback.classList.remove("hidden");
                    }
                }
            });
            section.querySelector("#site-delete-btn")?.addEventListener("click", async () => {
                if (!selectedSite) return;
                await fetchJSON(`/sites/${encodeURIComponent(selectedSite.site_id)}`, { method: "DELETE" });
                state.selectedSiteId = "";
                await renderWebsiteManagePage();
            });
        } catch (error) {
            section.innerHTML = `<div class="max-w-7xl mx-auto text-hud-red text-sm">Failed to load sites: ${escapeHtml(error.message)}</div>`;
        }
    }

    function facebookPageCard(page) {
        const isConnected = page.status === "connected";
        const tasks = (page.tasks || []).slice(0, 4);
        const group = page.group || "";
        const coverStyle = page.cover_url
            ? `background-image: linear-gradient(180deg, rgba(0,0,0,0.18), rgba(0,0,0,0.72)), url('${escapeHtml(page.cover_url)}'); background-size: cover; background-position: center;`
            : "background: radial-gradient(circle at top left, rgba(74,158,255,0.22), rgba(0,0,0,0.15));";
        return `
            <div class="facebook-page-card bg-black/40 border ${isConnected ? "hover:border-hud-fb" : "border-hud-red/40 hover:border-hud-red"} transition overflow-hidden" data-page-id="${escapeHtml(page.page_id || "")}" style="border-color: ${isConnected ? "rgba(74, 158, 255, 0.3)" : "rgba(255, 0, 60, 0.4)"};">
                <div class="h-28 border-b border-hud-fb/20 relative" style="${coverStyle}">
                    <div class="absolute top-3 right-3"><span class="badge ${isConnected ? "green" : "red"}"><span class="status-dot ${isConnected ? "green" : "red"}" style="width:5px;height:5px;"></span> ${isConnected ? "ACTIVE" : "ISSUE"}</span></div>
                </div>
                <div class="px-4 pb-4">
                <div class="flex items-end gap-3 mb-4">
                    <div class="w-14 h-14 -mt-7 rounded-full flex items-center justify-center flex-shrink-0 overflow-hidden shadow-[0_0_0_4px_rgba(0,0,0,0.75)]" style="background: rgba(74, 158, 255, 0.2); border: 2px solid ${isConnected ? "#4a9eff" : "#ff003c"};">
                        ${page.picture_url ? `<img src="${escapeHtml(page.picture_url)}" alt="${escapeHtml(page.name)}" class="w-full h-full object-cover"/>` : `<i class="fa-brands fa-facebook ${isConnected ? "text-hud-fb" : "text-hud-red"} text-lg"></i>`}
                    </div>
                    <div class="flex-1 min-w-0 pt-3">
                        <div class="flex items-center gap-2 mb-0.5">
                            <span class="font-display font-bold text-sm text-white uppercase-wide truncate">${escapeHtml(page.name || page.page_id)}</span>
                            ${isConnected ? `<i class="fa-solid fa-check text-[10px]" style="color:#4a9eff;"></i>` : ""}
                        </div>
                        <div class="text-[10px] text-hud-muted truncate">${escapeHtml(page.category || "Facebook Page")} · ${escapeHtml(page.page_id || "-")}</div>
                    </div>
                </div>
                <div class="py-3 border-t" style="border-color: rgba(74, 158, 255, 0.15);">
                    <button class="facebook-page-group-label badge ${group ? "amber" : "cyan"} mb-3 hover:brightness-125" data-page-id="${escapeHtml(page.page_id || "")}" data-page-name="${escapeHtml(page.name || page.page_id || "")}" data-current-group="${escapeHtml(group)}">
                        <i class="fa-solid fa-layer-group"></i> ${escapeHtml(group || "Chưa có nhóm")}
                    </button>
                    <div class="text-[9px] text-hud-muted uppercase-wide mb-2">PAGE TASKS</div>
                    <div class="flex flex-wrap gap-2">
                        ${tasks.map((task) => `<span class="badge cyan">${escapeHtml(task)}</span>`).join("") || `<span class="text-[10px] text-hud-muted">No task metadata</span>`}
                    </div>
                </div>
                <div class="grid grid-cols-2 gap-3 py-3 border-t text-[10px]" style="border-color: rgba(74, 158, 255, 0.15);">
                    <div><div class="text-hud-muted uppercase-wide">TOKEN</div><div class="font-mono text-white truncate">${escapeHtml(page.token_prefix || "-")}</div></div>
                    <div><div class="text-hud-muted uppercase-wide">CONNECTED</div><div class="text-white truncate">${escapeHtml(page.connected_at ? formatDate(page.connected_at) : "-")}</div></div>
                </div>
                </div>
            </div>
        `;
    }

    async function renderFacebookPagesPage() {
        const section = document.getElementById("page-fb-pages");
        if (!section) return;
        section.innerHTML = `<div class="max-w-7xl mx-auto text-hud-muted text-sm">Loading Facebook pages...</div>`;
        try {
            const [payload, groupPayload] = await Promise.all([
                fetchJSON("/facebook/pages"),
                fetchJSON("/facebook/page-groups").catch(() => ({ groups: [] })),
            ]);
            const pages = payload.pages || [];
            const savedGroups = Array.isArray(state.facebookPageGroups) ? state.facebookPageGroups : [];
            const persistedGroups = (groupPayload.groups || []).map((item) => String(item.name || "").trim()).filter(Boolean);
            let groups = [...new Set([...savedGroups, ...persistedGroups, ...pages.map((page) => String(page.group || "").trim()).filter(Boolean)])].sort((a, b) => a.localeCompare(b));
            state.facebookPageGroups = groups;
            const selectedGroup = state.facebookPageGroupFilter || "";
            const visiblePages = selectedGroup === "__ungrouped__"
                ? pages.filter((page) => !String(page.group || "").trim())
                : selectedGroup ? pages.filter((page) => String(page.group || "") === selectedGroup) : pages;
            const connectedCount = pages.filter((page) => page.status === "connected").length;
            const issueCount = pages.length - connectedCount;
            const taskCount = pages.reduce((sum, page) => sum + ((page.tasks || []).length), 0);
            const groupedCount = pages.filter((page) => String(page.group || "").trim()).length;
            section.innerHTML = `
                <div class="max-w-7xl mx-auto">
                    <div class="grid grid-cols-4 gap-4 mb-6">
                        <div class="hud-card p-4 fade-in" style="border-color: rgba(74, 158, 255, 0.3);"><span class="c-tl" style="border-color: #4a9eff;"></span><span class="c-br" style="border-color: #4a9eff;"></span><div class="text-[9px] uppercase-widest mb-1" style="color:#4a9eff;">TỔNG FANPAGE</div><div class="metric-num text-2xl text-white">${pages.length}</div></div>
                        <div class="hud-card green p-4 fade-in"><span class="c-tl"></span><span class="c-br"></span><div class="text-[9px] text-hud-green uppercase-widest mb-1">CONNECTED</div><div class="metric-num text-2xl text-hud-green">${connectedCount}</div></div>
                        <div class="hud-card p-4 fade-in"><span class="c-tl"></span><span class="c-br"></span><div class="text-[9px] text-hud-cyan uppercase-widest mb-1">GROUPED</div><div id="fb-pages-grouped-count" class="metric-num text-2xl text-white">${groupedCount}/${pages.length}</div></div>
                        <div class="hud-card danger p-4 fade-in"><span class="c-tl"></span><span class="c-br"></span><div class="text-[9px] text-hud-red uppercase-widest mb-1">ISSUES</div><div class="metric-num text-2xl text-hud-red">${issueCount}</div></div>
                    </div>
                    <div class="hud-card fade-in" style="border-color: rgba(74, 158, 255, 0.3);">
                        <span class="c-tl" style="border-color: #4a9eff;"></span><span class="c-br" style="border-color: #4a9eff;"></span>
                        <div class="header-strip px-5 py-3 flex items-center gap-2" style="background: linear-gradient(90deg, rgba(74, 158, 255, 0.15) 0%, rgba(74, 158, 255, 0.02) 50%, rgba(74, 158, 255, 0.15) 100%); border-bottom-color: rgba(74, 158, 255, 0.4);">
                            <i class="fa-solid fa-users-viewfinder text-hud-fb"></i>
                            <span class="font-display font-black text-xs text-white uppercase-widest">CONNECTED FANPAGES</span>
                            <select id="facebook-page-group-filter" class="hud-input ml-auto px-3 py-2 text-[10px] uppercase-wide">
                                <option value="">TẤT CẢ NHÓM</option>
                                <option value="__ungrouped__" ${selectedGroup === "__ungrouped__" ? "selected" : ""}>CHƯA CÓ NHÓM</option>
                                ${groups.map((group) => `<option value="${escapeHtml(group)}" ${group === selectedGroup ? "selected" : ""}>${escapeHtml(group)}</option>`).join("")}
                            </select>
                            <button id="fb-groups-open" class="btn-ghost px-3 py-2 text-[10px] uppercase-wide font-bold">
                                <i class="fa-solid fa-layer-group"></i> TẠO / QUẢN LÝ NHÓM
                            </button>
                            <button id="fb-connect-open" class="px-4 py-2 text-[10px] uppercase-wide font-bold" style="background: #4a9eff; color: #fff; border: 1px solid #4a9eff;">
                                <i class="fa-brands fa-facebook"></i> CONNECT NEW PAGE
                            </button>
                        </div>
                        <div class="p-5 grid grid-cols-2 gap-4">
                            ${visiblePages.map(facebookPageCard).join("") || `<div class="col-span-2 text-center py-10 text-hud-muted text-sm border border-hud-cyan/10 bg-black/30">${selectedGroup ? "Không có fanpage trong nhóm này." : "Chưa có fanpage nào. Bấm Connect New Page để nhập short-lived token."}</div>`}
                        </div>
                    </div>
                    <div id="fb-connect-dialog" class="fixed inset-0 z-50 hidden items-center justify-center bg-black/70 backdrop-blur-sm">
                        <div class="hud-card w-full max-w-2xl p-6 border-hud-fb/40">
                            <span class="c-tl" style="border-color:#4a9eff;"></span><span class="c-br" style="border-color:#4a9eff;"></span>
                            <div class="flex items-start justify-between gap-4 mb-5">
                                <div>
                                    <div class="flex items-center gap-2 text-hud-fb uppercase-wide text-xs font-bold"><i class="fa-brands fa-facebook"></i> CONNECT FACEBOOK PAGES</div>
                                    <h3 class="font-display font-black text-xl text-white uppercase-wide mt-1">Nhập short-lived user token</h3>
                                    <p class="text-[11px] text-hud-muted mt-2">Backend sẽ exchange sang long-lived user token, gọi /me/accounts và lưu page access token cho từng fanpage.</p>
                                </div>
                                <button id="fb-connect-close" class="btn-ghost px-3 py-2 text-xs"><i class="fa-solid fa-xmark"></i></button>
                            </div>
                            <div id="fb-connect-feedback" class="hidden text-[11px] border p-3 mb-4"></div>
                            <label class="text-[10px] font-bold text-hud-cyan uppercase-widest mb-2 block">Short-lived token</label>
                            <textarea id="fb-short-token" class="hud-input w-full min-h-[140px] px-4 py-3 text-xs font-mono" placeholder="EAAB..."></textarea>
                            <div class="mt-4 flex gap-2">
                                <button id="fb-connect-submit" class="flex-1 px-4 py-2.5 text-[10px] uppercase-wide font-bold" style="background:#4a9eff;color:#fff;border:1px solid #4a9eff;"><i class="fa-solid fa-link"></i> CONNECT & IMPORT PAGES</button>
                                <button id="fb-connect-cancel" class="btn-ghost px-4 py-2.5 text-[10px] uppercase-wide font-bold">CANCEL</button>
                            </div>
                        </div>
                    </div>
                    <div id="fb-group-dialog" class="fixed inset-0 z-50 hidden items-center justify-center bg-black/70 backdrop-blur-sm">
                        <div class="hud-card w-full max-w-xl p-6 border-hud-fb/40">
                            <span class="c-tl" style="border-color:#4a9eff;"></span><span class="c-br" style="border-color:#4a9eff;"></span>
                            <div class="flex items-start justify-between gap-4 mb-5">
                                <div>
                                    <div class="flex items-center gap-2 text-hud-fb uppercase-wide text-xs font-bold"><i class="fa-solid fa-layer-group"></i> PAGE GROUPS</div>
                                    <h3 id="fb-group-dialog-title" class="font-display font-black text-xl text-white uppercase-wide mt-1">Quản lý nhóm page</h3>
                                    <p id="fb-group-dialog-subtitle" class="text-[11px] text-hud-muted mt-2">Tạo nhóm theo chủ đề, rồi bấm label trên từng page để gán hoặc đổi nhóm.</p>
                                </div>
                                <button id="fb-group-close" class="btn-ghost px-3 py-2 text-xs"><i class="fa-solid fa-xmark"></i></button>
                            </div>
                            <div class="flex gap-2 mb-4">
                                <input id="fb-group-name" class="hud-input flex-1 px-4 py-2 text-xs" placeholder="Tên nhóm, ví dụ: Thuốc lào"/>
                                <button id="fb-group-create" class="px-4 py-2 text-[10px] uppercase-wide font-bold" style="background:#4a9eff;color:#fff;border:1px solid #4a9eff;">TẠO NHÓM</button>
                            </div>
                            <div id="fb-group-list" class="space-y-2 max-h-[320px] overflow-y-auto pr-1"></div>
                            <div class="mt-4 flex justify-between gap-2">
                                <button id="fb-group-clear-page" class="btn-ghost px-4 py-2.5 text-[10px] uppercase-wide font-bold hidden">BỎ NHÓM PAGE NÀY</button>
                                <button id="fb-group-cancel" class="btn-ghost px-4 py-2.5 text-[10px] uppercase-wide font-bold ml-auto">ĐÓNG</button>
                            </div>
                        </div>
                    </div>
                </div>
            `;

            const dialog = section.querySelector("#fb-connect-dialog");
            const feedback = section.querySelector("#fb-connect-feedback");
            const closeDialog = () => dialog?.classList.add("hidden");
            const openDialog = () => {
                feedback?.classList.add("hidden");
                dialog?.classList.remove("hidden");
                dialog?.classList.add("flex");
                section.querySelector("#fb-short-token")?.focus();
            };
            section.querySelector("#fb-connect-open")?.addEventListener("click", openDialog);
            section.querySelector("#facebook-page-group-filter")?.addEventListener("change", (event) => {
                state.facebookPageGroupFilter = event.target.value || "";
                renderFacebookPagesPage();
            });
            const groupDialog = section.querySelector("#fb-group-dialog");
            const groupTitle = section.querySelector("#fb-group-dialog-title");
            const groupSubtitle = section.querySelector("#fb-group-dialog-subtitle");
            const groupInput = section.querySelector("#fb-group-name");
            const groupList = section.querySelector("#fb-group-list");
            const clearPageGroupButton = section.querySelector("#fb-group-clear-page");
            let groupTargetPageId = "";
            let groupTargetPageName = "";
            const refreshGroupFilterOptions = () => {
                const filter = section.querySelector("#facebook-page-group-filter");
                if (!filter) return;
                const selected = filter.value || "";
                filter.innerHTML = `
                    <option value="">TẤT CẢ NHÓM</option>
                    <option value="__ungrouped__" ${selected === "__ungrouped__" ? "selected" : ""}>CHƯA CÓ NHÓM</option>
                    ${groups.map((group) => `<option value="${escapeHtml(group)}" ${group === selected ? "selected" : ""}>${escapeHtml(group)}</option>`).join("")}
                `;
            };
            const shouldHideForCurrentFilter = (group) => {
                const selected = state.facebookPageGroupFilter || "";
                if (!selected) return false;
                if (selected === "__ungrouped__") return Boolean(group);
                return selected !== group;
            };
            const updateGroupSummary = () => {
                const groupedNow = pages.filter((page) => String(page.group || "").trim()).length;
                const groupedMetric = section.querySelector("#fb-pages-grouped-count");
                if (groupedMetric) groupedMetric.textContent = `${groupedNow}/${pages.length}`;
            };
            const updatePageGroupDom = (pageId, group) => {
                const label = section.querySelector(`.facebook-page-group-label[data-page-id="${CSS.escape(pageId)}"]`);
                if (!label) return;
                label.dataset.currentGroup = group || "";
                label.classList.toggle("amber", Boolean(group));
                label.classList.toggle("cyan", !group);
                label.innerHTML = `<i class="fa-solid fa-layer-group"></i> ${escapeHtml(group || "Chưa có nhóm")}`;
                const card = label.closest(".facebook-page-card");
                if (card && shouldHideForCurrentFilter(group)) {
                    card.classList.add("hidden");
                }
            };
            const closeGroupDialog = () => {
                groupDialog?.classList.add("hidden");
                groupTargetPageId = "";
                groupTargetPageName = "";
            };
            const assignPageGroup = async (group) => {
                if (!groupTargetPageId) return;
                await fetchJSON(`/facebook/pages/${encodeURIComponent(groupTargetPageId)}/group`, {
                    method: "PATCH",
                    body: JSON.stringify({ group }),
                });
                const page = pages.find((item) => String(item.page_id || "") === groupTargetPageId);
                if (page) page.group = group;
                if (group && !state.facebookPageGroups.includes(group)) {
                    state.facebookPageGroups = [...state.facebookPageGroups, group].sort((a, b) => a.localeCompare(b));
                }
                if (group && !groups.includes(group)) {
                    groups = [...groups, group].sort((a, b) => a.localeCompare(b));
                    refreshGroupFilterOptions();
                }
                updatePageGroupDom(groupTargetPageId, group);
                updateGroupSummary();
                if (groupSubtitle) groupSubtitle.textContent = `${groupTargetPageName || groupTargetPageId} · hiện tại: ${group || "Chưa có nhóm"}`;
                renderGroupList();
            };
            const renderGroupList = () => {
                const counts = new Map();
                pages.forEach((page) => {
                    const group = String(page.group || "").trim();
                    if (group) counts.set(group, (counts.get(group) || 0) + 1);
                });
                if (!groupList) return;
                groupList.innerHTML = groups.map((group) => `
                    <div class="flex items-center gap-3 border border-hud-fb/15 bg-black/30 px-3 py-2">
                        <div class="flex-1 min-w-0">
                            <div class="text-xs text-white font-bold truncate">${escapeHtml(group)}</div>
                            <div class="text-[10px] text-hud-muted">${formatNumber(counts.get(group) || 0)} page</div>
                        </div>
                        ${groupTargetPageId ? `<button class="fb-group-assign btn-ghost px-3 py-1.5 text-[9px] uppercase-wide font-bold" data-group="${escapeHtml(group)}">GÁN</button>` : ""}
                    </div>
                `).join("") || `<div class="text-[11px] text-hud-muted border border-hud-fb/10 bg-black/30 p-4">Chưa có nhóm nào. Nhập tên nhóm ở trên để tạo.</div>`;
                groupList.querySelectorAll(".fb-group-assign").forEach((button) => {
                    button.addEventListener("click", () => assignPageGroup(button.dataset.group || ""));
                });
            };
            const openGroupDialog = ({ pageId = "", pageName = "", currentGroup = "" } = {}) => {
                groupTargetPageId = pageId;
                groupTargetPageName = pageName;
                if (groupTitle) groupTitle.textContent = pageId ? "Gán nhóm cho page" : "Quản lý nhóm page";
                if (groupSubtitle) groupSubtitle.textContent = pageId ? `${pageName || pageId} · hiện tại: ${currentGroup || "Chưa có nhóm"}` : "Tạo nhóm theo chủ đề, rồi bấm label trên từng page để gán hoặc đổi nhóm.";
                clearPageGroupButton?.classList.toggle("hidden", !pageId);
                if (groupInput) groupInput.value = "";
                renderGroupList();
                groupDialog?.classList.remove("hidden");
                groupDialog?.classList.add("flex");
                groupInput?.focus();
            };
            section.querySelector("#fb-groups-open")?.addEventListener("click", () => openGroupDialog());
            section.querySelectorAll(".facebook-page-group-label").forEach((button) => {
                button.addEventListener("click", () => {
                    openGroupDialog({
                        pageId: button.dataset.pageId || "",
                        pageName: button.dataset.pageName || "",
                        currentGroup: button.dataset.currentGroup || "",
                    });
                });
            });
            section.querySelector("#fb-group-create")?.addEventListener("click", async () => {
                const group = String(groupInput?.value || "").trim().replace(/\s+/g, " ");
                if (!group) return;
                await fetchJSON("/facebook/page-groups", {
                    method: "POST",
                    body: JSON.stringify({ name: group }),
                });
                if (!state.facebookPageGroups.includes(group)) {
                    state.facebookPageGroups = [...state.facebookPageGroups, group].sort((a, b) => a.localeCompare(b));
                }
                if (groupTargetPageId) {
                    await assignPageGroup(group);
                    return;
                }
                groups = [...new Set([...groups, group])].sort((a, b) => a.localeCompare(b));
                refreshGroupFilterOptions();
                renderGroupList();
                if (groupInput) groupInput.value = "";
            });
            groupInput?.addEventListener("keydown", (event) => {
                if (event.key === "Enter") {
                    event.preventDefault();
                    section.querySelector("#fb-group-create")?.click();
                }
            });
            clearPageGroupButton?.addEventListener("click", () => assignPageGroup(""));
            section.querySelector("#fb-group-close")?.addEventListener("click", closeGroupDialog);
            section.querySelector("#fb-group-cancel")?.addEventListener("click", closeGroupDialog);
            groupDialog?.addEventListener("click", (event) => {
                if (event.target === groupDialog) closeGroupDialog();
            });
            section.querySelector("#fb-connect-close")?.addEventListener("click", closeDialog);
            section.querySelector("#fb-connect-cancel")?.addEventListener("click", closeDialog);
            dialog?.addEventListener("click", (event) => {
                if (event.target === dialog) closeDialog();
            });
            section.querySelector("#fb-connect-submit")?.addEventListener("click", async () => {
                const token = section.querySelector("#fb-short-token")?.value.trim() || "";
                if (!token) {
                    if (feedback) {
                        feedback.className = "text-[11px] border p-3 mb-4 text-hud-red border-hud-red/30 bg-hud-red/10";
                        feedback.textContent = "Cần nhập short-lived token.";
                        feedback.classList.remove("hidden");
                    }
                    return;
                }
                try {
                    if (feedback) {
                        feedback.className = "text-[11px] border p-3 mb-4 text-hud-cyan border-hud-cyan/30 bg-hud-cyan/10";
                        feedback.textContent = "Đang exchange token và lấy danh sách page...";
                        feedback.classList.remove("hidden");
                    }
                    const result = await fetchJSON("/facebook/pages/connect", {
                        method: "POST",
                        body: JSON.stringify({ short_lived_token: token }),
                    });
                    if (feedback) {
                        feedback.className = "text-[11px] border p-3 mb-4 text-hud-green border-hud-green/30 bg-hud-green/10";
                        feedback.textContent = `Đã kết nối ${result.total} fanpage.`;
                        feedback.classList.remove("hidden");
                    }
                    await renderFacebookPagesPage();
                } catch (error) {
                    if (feedback) {
                        feedback.className = "text-[11px] border p-3 mb-4 text-hud-red border-hud-red/30 bg-hud-red/10";
                        feedback.textContent = `Connect failed: ${error.message}`;
                        feedback.classList.remove("hidden");
                    }
                }
            });
        } catch (error) {
            section.innerHTML = `<div class="max-w-7xl mx-auto text-hud-red text-sm">Failed to load Facebook pages: ${escapeHtml(error.message)}</div>`;
        }
    }

    function facebookStatsChart(series) {
        const items = series && series.length ? series : [];
        const width = 800;
        const height = 280;
        const left = 48;
        const right = 24;
        const top = 34;
        const bottom = 42;
        const maxValue = Math.max(1, ...items.map((item) => Math.max(Number(item.reach || 0), Number(item.engagement || 0))));
        const point = (item, index, key) => {
            const x = items.length <= 1 ? left : left + ((width - left - right) * index) / (items.length - 1);
            const y = top + (height - top - bottom) * (1 - Number(item[key] || 0) / maxValue);
            return `${x.toFixed(1)},${y.toFixed(1)}`;
        };
        const reachPoints = items.map((item, index) => point(item, index, "reach")).join(" ");
        const engagementPoints = items.map((item, index) => point(item, index, "engagement")).join(" ");
        const areaPoints = reachPoints ? `${reachPoints} ${width - right},${height - bottom} ${left},${height - bottom}` : "";
        const labels = items.map((item, index) => {
            const x = items.length <= 1 ? left : left + ((width - left - right) * index) / (items.length - 1);
            return `<text x="${x.toFixed(1)}" y="260">${escapeHtml(String(item.date || "").slice(5))}</text>`;
        }).join("");
        const dots = items.map((item, index) => {
            const [x, y] = point(item, index, "reach").split(",");
            return `<circle cx="${x}" cy="${y}" r="3"/>`;
        }).join("");
        const hoverTargets = items.map((item, index) => {
            const [reachX, reachY] = point(item, index, "reach").split(",").map(Number);
            const [, engagementY] = point(item, index, "engagement").split(",").map(Number);
            const tooltipX = Math.min(width - 190, Math.max(54, reachX - 78));
            const tooltipY = Math.max(46, Math.min(reachY, engagementY) - 54);
            return `
                <g class="fb-chart-hover">
                    <line x1="${reachX.toFixed(1)}" y1="${top}" x2="${reachX.toFixed(1)}" y2="${height - bottom}" stroke="#00f0ff" stroke-width="0.7" stroke-opacity="0" stroke-dasharray="4 5"/>
                    <circle cx="${reachX.toFixed(1)}" cy="${reachY.toFixed(1)}" r="8" fill="#4a9eff" fill-opacity="0"/>
                    <rect x="${tooltipX.toFixed(1)}" y="${tooltipY.toFixed(1)}" width="156" height="48" fill="rgba(0,0,0,0.9)" stroke="#00f0ff" stroke-opacity="0.55" class="fb-chart-tooltip"/>
                    <text x="${(tooltipX + 10).toFixed(1)}" y="${(tooltipY + 16).toFixed(1)}" fill="#ffffff" font-size="9" font-family="JetBrains Mono" class="fb-chart-tooltip">${escapeHtml(item.date || "")}</text>
                    <text x="${(tooltipX + 10).toFixed(1)}" y="${(tooltipY + 31).toFixed(1)}" fill="#4a9eff" font-size="9" font-family="JetBrains Mono" class="fb-chart-tooltip">Reach ${formatCompact(item.reach || 0)}</text>
                    <text x="${(tooltipX + 86).toFixed(1)}" y="${(tooltipY + 31).toFixed(1)}" fill="#22c55e" font-size="9" font-family="JetBrains Mono" class="fb-chart-tooltip">Eng ${formatCompact(item.engagement || 0)}</text>
                    <rect x="${Math.max(left, reachX - 14).toFixed(1)}" y="${top}" width="28" height="${height - top - bottom}" fill="transparent"/>
                </g>
            `;
        }).join("");
        return `
            <svg viewBox="0 0 ${width} ${height}" class="w-full h-56">
                <style>
                    .fb-chart-tooltip { opacity: 0; pointer-events: none; transition: opacity 0.12s ease; }
                    .fb-chart-hover:hover .fb-chart-tooltip { opacity: 1; }
                    .fb-chart-hover:hover line { stroke-opacity: 0.65; }
                    .fb-chart-hover:hover circle { fill-opacity: 0.18; }
                </style>
                <defs><linearGradient id="fbReachArea" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="#4a9eff" stop-opacity="0.35"/><stop offset="100%" stop-color="#4a9eff" stop-opacity="0"/></linearGradient></defs>
                <g stroke="rgba(74,158,255,0.1)" stroke-width="0.5">
                    <line x1="40" y1="40" x2="780" y2="40"/><line x1="40" y1="100" x2="780" y2="100"/><line x1="40" y1="160" x2="780" y2="160"/><line x1="40" y1="220" x2="780" y2="220"/>
                </g>
                <g fill="#8a9bb3" font-size="9" font-family="JetBrains Mono" text-anchor="middle">${labels}</g>
                ${areaPoints ? `<polygon points="${areaPoints}" fill="url(#fbReachArea)"/>` : ""}
                ${reachPoints ? `<polyline points="${reachPoints}" fill="none" stroke="#4a9eff" stroke-width="2" style="filter: drop-shadow(0 0 4px #4a9eff);"/>` : ""}
                ${engagementPoints ? `<polyline points="${engagementPoints}" fill="none" stroke="#22c55e" stroke-width="1.5" stroke-dasharray="5 5"/>` : ""}
                <g fill="#4a9eff">${dots}</g>
                ${hoverTargets}
            </svg>
        `;
    }

    async function renderFacebookStatsPage() {
        const section = document.getElementById("page-fb-stats");
        if (!section) return;
        if (!section.dataset.hydrated) {
            section.innerHTML = `<div class="max-w-7xl mx-auto text-hud-muted text-sm">Đang đọc cache thống kê Facebook...</div>`;
        }
        try {
            const controller = new AbortController();
            const timeout = window.setTimeout(() => controller.abort(), 15000);
            const stats = await fetchJSON("/facebook/stats?days=7", { signal: controller.signal });
            window.clearTimeout(timeout);
            const totals = stats.totals || {};
            const topPosts = stats.top_posts || [];
            const contentPerformance = stats.content_performance || [];
            const analyticsBreakdown = stats.analytics_breakdown || {};
            const analyticsErrorTypes = stats.analytics_error_types || {};
            const maxAvgReach = Math.max(1, ...contentPerformance.map((item) => Number(item.avg_reach || 0)));
            section.dataset.hydrated = "1";
            section.innerHTML = `
                <div class="max-w-7xl mx-auto">
                    <div id="fb-stats-sync-status" class="${state.facebookStatsSyncing ? "" : "hidden"} mb-4 border border-hud-cyan/30 bg-hud-cyan/10 text-hud-cyan text-[11px] p-3">
                        Đang đồng bộ thống kê Facebook trong nền. Dữ liệu cache vẫn hiển thị, sync xong sẽ tự cập nhật.
                    </div>
                    <div class="grid grid-cols-6 gap-4 mb-6">
                        ${[
                            ["TOTAL REACH", formatCompact(totals.reach), "white", "hud-fb"],
                            ["ENGAGEMENT", formatCompact(totals.engagement), "white", "hud-cyan"],
                            ["REACTIONS", formatCompact(totals.reactions || totals.likes || 0), "white", "hud-cyan"],
                            ["SHARES", formatCompact(totals.shares), "white", "hud-cyan"],
                            ["COMMENTS", formatCompact(totals.comments), "white", "hud-cyan"],
                            ["ENG / REACH", `${formatNumber(totals.ctr || 0, 2)}%`, "white", "hud-cyan"],
                        ].map(([label, value, tone, color]) => `
                            <div class="hud-card p-4 fade-in" style="border-color: rgba(74, 158, 255, 0.3);">
                                <span class="c-tl" style="border-color: #4a9eff;"></span><span class="c-br" style="border-color: #4a9eff;"></span>
                                <div class="text-[9px] ${color === "hud-fb" ? "" : `text-${color}`} uppercase-widest mb-1" ${color === "hud-fb" ? `style="color:#4a9eff;"` : ""}>${label}</div>
                                <div class="metric-num text-2xl text-${tone}">${value}</div>
                                <div class="text-[9px] text-hud-muted">${formatNumber(stats.page_count || 0)} pages · ${formatNumber(totals.posts || 0)} posts</div>
                            </div>
                        `).join("")}
                    </div>
                    <div class="hud-card mb-6 p-4 fade-in" style="border-color: rgba(74, 158, 255, 0.25);">
                        <span class="c-tl" style="border-color:#4a9eff;"></span><span class="c-br" style="border-color:#4a9eff;"></span>
                        <div class="flex flex-wrap items-center gap-3 text-[10px] uppercase-wide">
                            <span class="text-hud-muted">ANALYTICS COVERAGE</span>
                            <span class="badge green">${formatNumber(totals.analytics_coverage || 0, 1)}%</span>
                            <span class="badge green">available ${formatNumber(totals.analytics_available || analyticsBreakdown.available || 0)}</span>
                            <span class="badge amber">partial ${formatNumber(totals.analytics_partial || analyticsBreakdown.partial || 0)}</span>
                            <span class="badge amber">stale ${formatNumber(totals.analytics_stale || analyticsBreakdown.stale || 0)}</span>
                            <span class="badge red">error ${formatNumber(totals.analytics_error || analyticsBreakdown.error || 0)}</span>
                            <span class="badge cyan">empty ${formatNumber(totals.analytics_empty || analyticsBreakdown.empty || 0)}</span>
                            ${Object.keys(analyticsErrorTypes).length ? `<span class="text-hud-muted">errors: ${escapeHtml(Object.entries(analyticsErrorTypes).map(([key, value]) => `${key} ${value}`).join(" · "))}</span>` : ""}
                        </div>
                    </div>

                    ${(stats.warnings || []).length ? `<div class="mb-5 border border-hud-amber/30 bg-hud-amber/10 text-hud-amber text-[11px] p-3">Một số metric không lấy được do quyền Meta API hoặc page không có dữ liệu: ${escapeHtml(redactSensitiveText((stats.warnings || []).slice(0, 3).join(" | ")))}</div>` : ""}

                    <div class="grid grid-cols-3 gap-5 mb-6">
                        <div class="col-span-2 hud-card fade-in" style="border-color: rgba(74, 158, 255, 0.3);">
                            <span class="c-tl" style="border-color: #4a9eff;"></span><span class="c-br" style="border-color: #4a9eff;"></span>
                            <div class="header-strip px-5 py-3 flex items-center gap-2" style="background: linear-gradient(90deg, rgba(74, 158, 255, 0.15) 0%, rgba(74, 158, 255, 0.02) 50%, rgba(74, 158, 255, 0.15) 100%); border-bottom-color: rgba(74, 158, 255, 0.4);">
                                <i class="fa-solid fa-chart-area text-hud-fb"></i>
                                <span class="font-display font-black text-xs text-white uppercase-widest">AGGREGATED REACH & ENGAGEMENT · ${formatNumber(stats.days || 7)} DAYS</span>
                                <button id="fb-stats-refresh" class="ml-auto btn-ghost px-3 py-1.5 text-[10px] uppercase-wide font-bold"><i class="fa-solid fa-rotate"></i> REFRESH</button>
                            </div>
                            <div class="p-5">${facebookStatsChart(stats.series || [])}</div>
                        </div>
                        <div class="hud-card fade-in" style="border-color: rgba(74, 158, 255, 0.3);">
                            <span class="c-tl" style="border-color: #4a9eff;"></span><span class="c-br" style="border-color: #4a9eff;"></span>
                            <div class="header-strip px-5 py-3 flex items-center gap-2" style="background: linear-gradient(90deg, rgba(74, 158, 255, 0.15) 0%, rgba(74, 158, 255, 0.02) 50%, rgba(74, 158, 255, 0.15) 100%); border-bottom-color: rgba(74, 158, 255, 0.4);">
                                <i class="fa-solid fa-trophy text-hud-amber"></i>
                                <span class="font-display font-black text-xs text-white uppercase-widest">TOP POSTS · ALL PAGES</span>
                            </div>
                            <div class="p-4 space-y-3">
                                ${topPosts.map((post, index) => `
                                    <div class="border-l-2 ${index === 0 ? "border-hud-amber" : ""} pl-3" style="${index === 0 ? "" : "border-color:#4a9eff;"}">
                                        <div class="text-[9px] text-hud-muted uppercase-wide">${escapeHtml(post.page_name || "Facebook page")}</div>
                                        <div class="text-[11px] text-white font-bold truncate">${escapeHtml(post.message || "Untitled post")}</div>
                                        <div class="text-[10px] ${index === 0 ? "text-hud-amber" : ""}" ${index === 0 ? "" : `style="color:#4a9eff;"`}><i class="fa-solid fa-eye"></i> ${formatCompact(post.reach)} · <i class="fa-solid fa-heart"></i> ${formatCompact(post.engagement)} · <i class="fa-solid fa-thumbs-up"></i> ${formatCompact(post.reactions || 0)} · <i class="fa-solid fa-comment"></i> ${formatCompact(post.comments)}</div>
                                    </div>
                                `).join("") || `<div class="text-[11px] text-hud-muted">Chưa có dữ liệu top post.</div>`}
                            </div>
                        </div>
                    </div>

                    <div class="grid grid-cols-2 gap-5">
                        <div class="hud-card fade-in" style="border-color: rgba(74, 158, 255, 0.3);">
                            <span class="c-tl" style="border-color: #4a9eff;"></span><span class="c-br" style="border-color: #4a9eff;"></span>
                            <div class="header-strip px-5 py-3 flex items-center gap-2" style="background: linear-gradient(90deg, rgba(74, 158, 255, 0.15) 0%, rgba(74, 158, 255, 0.02) 50%, rgba(74, 158, 255, 0.15) 100%); border-bottom-color: rgba(74, 158, 255, 0.4);">
                                <i class="fa-solid fa-clock text-hud-fb"></i>
                                <span class="font-display font-black text-xs text-white uppercase-widest">BEST POSTING TIME · ALL PAGES</span>
                            </div>
                            <div class="p-5 text-center">
                                <div class="text-[9px] text-hud-muted uppercase-widest">PEAK TIME</div>
                                <div class="metric-num text-3xl" style="color:#4a9eff;">${escapeHtml(stats.best_posting_time || "N/A")}</div>
                                <div class="text-[10px] text-hud-muted">Tính theo engagement của các bài gần nhất.</div>
                            </div>
                        </div>
                        <div class="hud-card fade-in" style="border-color: rgba(74, 158, 255, 0.3);">
                            <span class="c-tl" style="border-color: #4a9eff;"></span><span class="c-br" style="border-color: #4a9eff;"></span>
                            <div class="header-strip px-5 py-3 flex items-center gap-2" style="background: linear-gradient(90deg, rgba(74, 158, 255, 0.15) 0%, rgba(74, 158, 255, 0.02) 50%, rgba(74, 158, 255, 0.15) 100%); border-bottom-color: rgba(74, 158, 255, 0.4);">
                                <i class="fa-solid fa-chart-column text-hud-fb"></i>
                                <span class="font-display font-black text-xs text-white uppercase-widest">CONTENT TYPE PERFORMANCE · ALL PAGES</span>
                            </div>
                            <div class="p-5 space-y-4">
                                ${contentPerformance.map((item) => {
                                    const pct = Math.max(4, Math.round((Number(item.avg_reach || 0) / maxAvgReach) * 100));
                                    return `
                                        <div>
                                            <div class="flex justify-between text-[10px] mb-1.5">
                                                <span class="text-white font-bold uppercase-wide">${escapeHtml(item.type || "UNKNOWN")}</span>
                                                <span class="font-bold" style="color:#4a9eff;">avg ${formatCompact(item.avg_reach)} reach · ${formatNumber(item.posts)} posts</span>
                                            </div>
                                            <div class="h-3" style="background: rgba(74, 158, 255, 0.1);"><div class="h-full" style="background:#4a9eff; width:${pct}%; box-shadow: 0 0 4px #4a9eff;"></div></div>
                                        </div>
                                    `;
                                }).join("") || `<div class="text-[11px] text-hud-muted">Chưa có dữ liệu performance theo loại nội dung.</div>`}
                            </div>
                        </div>
                    </div>
                </div>
            `;
            const syncStats = async () => {
                if (state.facebookStatsSyncing) return;
                state.facebookStatsSyncing = true;
                section.querySelector("#fb-stats-sync-status")?.classList.remove("hidden");
                const button = section.querySelector("#fb-stats-refresh");
                if (button) button.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> SYNCING`;
                try {
                    await fetchJSON("/facebook/stats/sync?days=7", { method: "POST" });
                } finally {
                    state.facebookStatsSyncing = false;
                    await renderFacebookStatsPage();
                }
            };
            section.querySelector("#fb-stats-refresh")?.addEventListener("click", syncStats);
            if (!stats.cached && !Number(totals.posts || 0) && !state.facebookStatsAutoSynced) {
                state.facebookStatsAutoSynced = true;
                setTimeout(syncStats, 50);
            }
        } catch (error) {
            const message = error.name === "AbortError" ? "Facebook stats request timed out. Có thể Graph API đang chậm hoặc thiếu quyền read_insights." : error.message;
            section.innerHTML = `<div class="max-w-7xl mx-auto text-hud-red text-sm">Failed to load Facebook stats: ${escapeHtml(message)}</div>`;
        }
    }

    function facebookWarningBanner(warnings) {
        const items = warnings || [];
        if (!items.length) return "";
        return `<div class="mb-5 border border-hud-amber/30 bg-hud-amber/10 text-hud-amber text-[11px] p-3">Một số dữ liệu Facebook không lấy được do quyền Meta API hoặc page không có dữ liệu: ${escapeHtml(redactSensitiveText(items.slice(0, 3).join(" | ")))}</div>`;
    }

    function attachmentLabel(attachment) {
        const type = String(attachment?.type || "file").toUpperCase();
        const mime = attachment?.mime_type ? ` · ${attachment.mime_type}` : "";
        return `${type}${mime}`;
    }

    function attachmentUrlLooksLikeImage(attachment) {
        const url = String(attachment?.preview_url || attachment?.url || "").toLowerCase();
        return [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"].some((ext) => url.includes(ext));
    }

    function isImageAttachment(attachment) {
        const type = String(attachment?.type || "").toLowerCase();
        const mime = String(attachment?.mime_type || "").toLowerCase();
        return Boolean((attachment?.preview_url || attachment?.url) && (type === "image" || mime.startsWith("image/") || attachmentUrlLooksLikeImage(attachment)));
    }

    function isVideoAttachment(attachment) {
        const type = String(attachment?.type || "").toLowerCase();
        const mime = String(attachment?.mime_type || "").toLowerCase();
        const url = String(attachment?.url || "").toLowerCase();
        return Boolean(attachment?.url && (type === "video" || mime.startsWith("video/") || [".mp4", ".mov", ".webm", ".m4v", ".avi"].some((ext) => url.includes(ext))));
    }

    function isAudioAttachment(attachment) {
        const type = String(attachment?.type || "").toLowerCase();
        const mime = String(attachment?.mime_type || "").toLowerCase();
        const url = String(attachment?.url || "").toLowerCase();
        return Boolean(attachment?.url && (type === "audio" || mime.startsWith("audio/") || [".mp3", ".wav", ".m4a", ".ogg"].some((ext) => url.includes(ext))));
    }

    function renderMessageAttachments(message) {
        const attachments = Array.isArray(message?.attachments) ? message.attachments : [];
        if (!attachments.length) return "";
        return `<div class="mt-2 space-y-2">
            ${attachments.map((attachment) => {
                const url = String(attachment.url || attachment.preview_url || "");
                const previewUrl = String(attachment.preview_url || attachment.url || "");
                const name = attachment.name || attachmentLabel(attachment);
                if (isImageAttachment(attachment) && previewUrl) {
                    return `<a href="${escapeHtml(url || previewUrl)}" target="_blank" rel="noopener noreferrer" class="block group">
                        <img src="${escapeHtml(previewUrl)}" alt="${escapeHtml(name)}" loading="lazy" class="max-h-64 rounded border border-hud-fb/40 object-cover bg-black/50"/>
                        <div class="text-[9px] mt-1 text-hud-fb uppercase-wide"><i class="fa-solid fa-image"></i> ${escapeHtml(name)}</div>
                    </a>`;
                }
                if (isVideoAttachment(attachment) && url) {
                    return `<div class="border border-hud-fb/40 bg-black/50 p-2">
                        <video src="${escapeHtml(url)}" controls class="max-h-64 max-w-full bg-black"></video>
                        <div class="text-[9px] mt-1 text-hud-fb uppercase-wide"><i class="fa-solid fa-video"></i> ${escapeHtml(name)}</div>
                    </div>`;
                }
                if (isAudioAttachment(attachment) && url) {
                    return `<div class="border border-hud-cyan/30 bg-black/50 p-2">
                        <audio src="${escapeHtml(url)}" controls class="w-64 max-w-full"></audio>
                        <div class="text-[9px] mt-1 text-hud-cyan uppercase-wide"><i class="fa-solid fa-microphone"></i> ${escapeHtml(name)}</div>
                    </div>`;
                }
                return `<a href="${escapeHtml(url || "#")}" target="_blank" rel="noopener noreferrer" class="flex items-center gap-3 border border-hud-cyan/30 bg-black/50 px-3 py-2 text-white/85 hover:border-hud-fb/60">
                    <i class="fa-solid fa-paperclip text-hud-cyan"></i>
                    <span class="min-w-0">
                        <span class="block text-[11px] font-bold truncate">${escapeHtml(name || "Attachment")}</span>
                        <span class="block text-[9px] text-hud-muted uppercase-wide">${escapeHtml(attachmentLabel(attachment))}</span>
                    </span>
                </a>`;
            }).join("")}
        </div>`;
    }

    function renderReplyQuote(message) {
        const replyTo = message?.reply_to || {};
        if (!replyTo.mid) return "";
        const quoteAttachments = Array.isArray(replyTo.attachments) ? replyTo.attachments : [];
        const imageAttachment = quoteAttachments.find((item) => isImageAttachment(item));
        const videoAttachment = quoteAttachments.find((item) => isVideoAttachment(item));
        const audioAttachment = quoteAttachments.find((item) => isAudioAttachment(item));
        const fileAttachment = quoteAttachments.find((item) => item && !isImageAttachment(item) && !isVideoAttachment(item) && !isAudioAttachment(item));
        const quoteLabel = imageAttachment
            ? "Quote · ảnh của bạn"
            : videoAttachment
                ? "Quote · video"
                : audioAttachment
                    ? "Quote · audio"
                    : fileAttachment
                        ? "Quote · tệp đính kèm"
                        : "Quote · tin nhắn";
        const quoteMeta = replyTo.created_time ? formatDate(replyTo.created_time) : (replyTo.from_name || "");
        const thumbUrl = imageAttachment ? String(imageAttachment.preview_url || imageAttachment.url || "") : "";
        const mediaUrl = imageAttachment ? String(imageAttachment.url || imageAttachment.preview_url || "") : "";
        return `<div class="reply-msg__quote-inner">
            <div class="reply-msg__thumb ${mediaUrl ? "is-media" : ""}">
                ${thumbUrl ? `<i class="fa-solid fa-image text-hud-fb text-sm"></i>` : `<i class="fa-solid fa-paperclip text-hud-fb text-sm"></i>`}
            </div>
            <div class="reply-msg__quote-info">
                <div class="reply-msg__quote-label"><i class="fa-solid fa-reply"></i> ${escapeHtml(quoteLabel)}</div>
                <div class="reply-msg__quote-meta">${escapeHtml(quoteMeta || replyTo.fallback_label || "Tin nhắn trước đó")}</div>
            </div>
        </div>
        ${mediaUrl ? `<a href="${escapeHtml(mediaUrl)}" target="_blank" rel="noopener noreferrer" class="reply-msg__quote-media block">
            <img src="${escapeHtml(mediaUrl)}" alt="${escapeHtml(quoteLabel)}" loading="lazy" />
        </a>` : ""}`;
    }

    function renderStandardMessageBody(message) {
        return `
            ${message.message ? `
                <div class="px-4 py-2 text-[12px] text-white ${message.direction === "outbound" ? "" : "bg-black/50 border border-hud-cyan/20"}" style="${message.direction === "outbound" ? "background: rgba(74, 158, 255, 0.2); border: 1px solid rgba(74, 158, 255, 0.5);" : ""}">
                    ${escapeHtml(message.message)}
                </div>
            ` : ""}
            ${renderMessageAttachments(message)}
            ${!message.message && !(message.attachments || []).length ? renderMessageFallback(message) : ""}
        `;
    }

    function renderReplyMessage(message) {
        const replyQuote = renderReplyQuote(message);
        if (!replyQuote) return renderStandardMessageBody(message);
        const hasBody = Boolean(message.message || (message.attachments || []).length || message.fallback_label);
        return `<div class="reply-msg__quote ${message.direction === "outbound" ? "is-outbound" : ""}">
            <span class="reply-msg__corner reply-msg__corner--tl"></span>
            <span class="reply-msg__corner reply-msg__corner--tr"></span>
            <span class="reply-msg__corner reply-msg__corner--bl"></span>
            <span class="reply-msg__corner reply-msg__corner--br"></span>
            ${replyQuote}
            ${hasBody ? `<div class="reply-msg__reply">
                ${message.message ? `<div class="reply-msg__bubble">${escapeHtml(message.message)}</div>` : ""}
                ${renderMessageAttachments(message)}
                ${!message.message && !(message.attachments || []).length ? renderMessageFallback(message) : ""}
            </div>` : ""}
        </div>`;
    }

    function renderMessageFallback(message) {
        const label = String(message?.fallback_label || "").trim();
        if (!label) return "";
        const normalized = label.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
        const icon = normalized.includes("sticker")
            ? "fa-face-smile"
            : normalized.includes("lien ket")
                ? "fa-link"
                : normalized.includes("dinh kem")
                    ? "fa-paperclip"
                    : "fa-envelope-open-text";
        return `<div class="px-4 py-2 text-[11px] text-hud-cyan bg-black/50 border border-hud-cyan/20 uppercase-wide">
            <i class="fa-solid ${icon}"></i> ${escapeHtml(label)}
        </div>`;
    }

    function scrollFacebookMessagesToBottom(section) {
        const list = section?.querySelector("#fb-message-thread");
        if (!list) return;
        const scroll = () => {
            list.scrollTop = list.scrollHeight;
        };
        requestAnimationFrame(scroll);
        setTimeout(scroll, 120);
        list.querySelectorAll("img").forEach((image) => {
            if (!image.complete) image.addEventListener("load", scroll, { once: true });
        });
    }

    const DEFAULT_FACEBOOK_MESSAGE_SLASH_COMMANDS = [
        { command: "/gia", label: "Hỏi nhu cầu / báo giá", text: "Anh/chị muốn em gửi báo giá mẫu nào ạ?" },
        { command: "/ship", label: "Giao hàng", text: "Bên em có giao hàng toàn quốc, anh/chị nhận hàng kiểm tra rồi thanh toán ạ." },
        { command: "/zalo", label: "Xin Zalo/SĐT", text: "Anh/chị cho em xin số Zalo để em gửi hình và tư vấn nhanh hơn nhé." },
        { command: "/camon", label: "Cảm ơn", text: "Em cảm ơn anh/chị đã quan tâm. Anh/chị cần thêm hình/video mẫu nào em gửi ngay ạ." },
        { command: "/chot", label: "Chốt đơn", text: "Nếu anh/chị chốt mẫu này, anh/chị gửi giúp em tên, số điện thoại và địa chỉ nhận hàng nhé." },
    ];

    function facebookSlashCommands() {
        return Array.isArray(state.facebookSlashCommands) ? state.facebookSlashCommands : DEFAULT_FACEBOOK_MESSAGE_SLASH_COMMANDS;
    }

    function persistFacebookSlashCommands(commands) {
        state.facebookSlashCommands = commands;
    }

    async function loadFacebookSlashCommands(force = false) {
        if (state.facebookSlashCommandsLoaded && !force) return facebookSlashCommands();
        const payload = await fetchJSON("/facebook/slash-commands");
        persistFacebookSlashCommands(payload.commands || []);
        state.facebookSlashCommandsLoaded = true;
        return facebookSlashCommands();
    }

    async function saveFacebookSlashCommand(command, label, text, originalCommand = "") {
        const payload = await fetchJSON("/facebook/slash-commands", {
            method: "POST",
            body: JSON.stringify({ command, label, text, original_command: originalCommand }),
        });
        persistFacebookSlashCommands(payload.commands || []);
        state.facebookSlashCommandsLoaded = true;
        return payload;
    }

    async function removeFacebookSlashCommand(command) {
        const payload = await fetchJSON(`/facebook/slash-commands?command=${encodeURIComponent(command)}`, { method: "DELETE" });
        persistFacebookSlashCommands(payload.commands || []);
        state.facebookSlashCommandsLoaded = true;
        return payload;
    }

    function facebookSlashQuery(value) {
        const text = String(value || "");
        const match = text.match(/(?:^|\s)\/([\p{L}\p{N}_-]*)$/u);
        if (match) return match[1].toLowerCase();
        const normalized = text.trim().toLowerCase();
        if (normalized.length >= 2) return normalized;
        return "";
    }

    function facebookSlashMatches(value) {
        const query = facebookSlashQuery(value);
        if (!query && !String(value || "").trim().endsWith("/")) return [];
        return facebookSlashCommands().filter((item) => {
            const haystack = `${item.command} ${item.label} ${item.text}`.toLowerCase();
            return !query || haystack.includes(query);
        }).slice(0, 5);
    }

    function renderFacebookSlashMenu(inputValue = "") {
        const matches = facebookSlashMatches(inputValue);
        const active = Boolean(facebookSlashQuery(inputValue) || String(inputValue || "").trim().endsWith("/"));
        return `<div id="fb-message-slash-menu" class="${active ? "" : "hidden"} border border-hud-cyan/25 bg-black/70 p-2 text-[11px]">
            ${renderFacebookSlashMenuContent(matches)}
        </div>`;
    }

    function renderFacebookSlashMenuContent(matches) {
        return `<div class="flex items-center gap-2 mb-2">
            <div class="text-[9px] uppercase-widest font-bold text-hud-cyan"><i class="fa-solid fa-terminal"></i> Slash menu</div>
            <button class="fb-slash-manage ml-auto btn-ghost px-2 py-1 text-[9px] uppercase-wide font-bold" type="button"><i class="fa-solid fa-gear"></i> Quản lý</button>
        </div>
        <div class="space-y-1">
            ${matches.map((item) => `
                <div class="relative group">
                    <button class="fb-message-slash-item w-full text-left px-3 py-2 pr-16 border border-hud-cyan/10 hover:border-hud-fb/50 hover:bg-hud-fb/10" data-command="${escapeHtml(item.command)}">
                        <span class="font-mono text-hud-fb font-bold">${escapeHtml(item.command)}</span>
                        <span class="text-white font-bold ml-2">${escapeHtml(item.label)}</span>
                        <span class="block text-hud-muted mt-0.5 truncate">${escapeHtml(item.text)}</span>
                    </button>
                    <div class="absolute inset-y-0 right-2 flex items-center gap-1 opacity-90 group-hover:opacity-100">
                        <button class="fb-slash-edit h-7 w-7 border border-hud-fb/35 bg-hud-fb/10 text-[10px] text-hud-fb hover:bg-hud-fb/20 hover:border-hud-fb" data-command="${escapeHtml(item.command)}" title="Sửa"><i class="fa-solid fa-pen"></i></button>
                        <button class="fb-slash-delete h-7 w-7 border border-hud-red/35 bg-hud-red/10 text-[10px] text-hud-red hover:bg-hud-red/20 hover:border-hud-red" data-command="${escapeHtml(item.command)}" title="Xóa"><i class="fa-solid fa-trash"></i></button>
                    </div>
                </div>
            `).join("") || `<div class="px-3 py-2 border border-hud-cyan/10 text-hud-muted">Chưa có mẫu phù hợp. Bấm Quản lý để thêm mẫu mới.</div>`}
        </div>`;
    }

    function updateFacebookSlashMenu(section) {
        const input = section?.querySelector("#fb-message-input");
        const menu = section?.querySelector("#fb-message-slash-menu");
        if (!input || !menu) return;
        const matches = facebookSlashMatches(input.value);
        const active = Boolean(facebookSlashQuery(input.value) || String(input.value || "").trim().endsWith("/"));
        menu.classList.toggle("hidden", !active);
        menu.innerHTML = renderFacebookSlashMenuContent(matches);
    }

    function applyFacebookSlashCommand(section, command) {
        const item = facebookSlashCommands().find((entry) => entry.command === command);
        const input = section?.querySelector("#fb-message-input");
        if (!item || !input) return;
        input.value = String(input.value || "").replace(/(?:^|\s)\/[\p{L}\p{N}_-]*$/u, "");
        input.value = `${input.value.trim() ? `${input.value.trim()} ` : ""}${item.text}`;
        input.focus();
        updateFacebookSlashMenu(section);
    }

    function renderFacebookSlashManagerDialog() {
        const commands = facebookSlashCommands();
        const editing = commands.find((item) => item.command === state.facebookSlashEditingCommand) || {};
        return `<div id="fb-slash-dialog" class="fixed inset-0 bg-black/75 backdrop-blur-md flex items-center justify-center p-4" style="z-index:9999;">
            <div class="hud-card w-full max-w-4xl p-0 relative overflow-hidden" style="border-color: rgba(0, 240, 255, 0.35); box-shadow: 0 0 40px rgba(0, 240, 255, 0.12);">
                <span class="c-tl"></span><span class="c-br"></span>
                <div class="header-strip px-5 py-4 flex items-center gap-3" style="background: linear-gradient(90deg, rgba(0, 240, 255, 0.16) 0%, rgba(74, 158, 255, 0.04) 55%, rgba(0, 240, 255, 0.1) 100%);">
                    <div class="h-10 w-10 border border-hud-cyan/40 bg-hud-cyan/10 flex items-center justify-center text-hud-cyan">
                        <i class="fa-solid fa-terminal"></i>
                    </div>
                    <div>
                        <div class="font-display text-white text-sm font-black uppercase-widest tracking-[0.22em]">Quản lý slash menu</div>
                        <div class="text-[10px] text-hud-muted">Thêm, sửa hoặc xóa mẫu trả lời nhanh cho inbox Facebook.</div>
                    </div>
                    <button id="fb-slash-close" class="ml-auto h-9 w-9 border border-white/10 bg-black/30 text-hud-muted hover:text-white hover:border-hud-cyan/50 text-xs"><i class="fa-solid fa-xmark"></i></button>
                </div>
                <div class="grid md:grid-cols-[1fr_1.15fr] gap-0">
                    <div class="p-5 border-r border-hud-cyan/10 bg-black/20">
                        <div class="flex items-center justify-between mb-3">
                            <div class="text-[10px] uppercase-wide text-hud-cyan font-bold">Mẫu hiện có</div>
                            <span class="badge cyan">${formatNumber(commands.length)} items</span>
                        </div>
                        <div class="space-y-2 max-h-[440px] overflow-y-auto pr-1">
                        ${commands.map((item) => `
                            <div class="border border-hud-cyan/15 bg-black/35 p-3 hover:border-hud-fb/40">
                                <div class="flex items-center gap-2">
                                    <span class="font-mono text-hud-fb font-bold">${escapeHtml(item.command)}</span>
                                    <span class="text-white text-xs font-bold truncate">${escapeHtml(item.label)}</span>
                                    <button class="fb-slash-dialog-edit ml-auto h-7 w-7 border border-hud-fb/25 bg-hud-fb/10 text-[10px] text-hud-fb hover:border-hud-fb" data-command="${escapeHtml(item.command)}"><i class="fa-solid fa-pen"></i></button>
                                    <button class="fb-slash-dialog-delete h-7 w-7 border border-hud-red/25 bg-hud-red/10 text-[10px] text-hud-red hover:border-hud-red" data-command="${escapeHtml(item.command)}"><i class="fa-solid fa-trash"></i></button>
                                </div>
                                <div class="text-[10px] text-hud-muted mt-1">${escapeHtml(item.text)}</div>
                            </div>
                        `).join("") || `<div class="border border-hud-cyan/10 bg-black/25 p-4 text-xs text-hud-muted">Chưa có mẫu nào. Tạo mẫu đầu tiên ở form bên phải.</div>`}
                        </div>
                    </div>
                    <form id="fb-slash-form" class="p-5 space-y-4 bg-gradient-to-b from-hud-panel/60 to-black/20">
                        <input type="hidden" id="fb-slash-original" value="${escapeHtml(editing.command || "")}">
                        <div class="flex items-center justify-between">
                            <div>
                                <div class="text-[10px] uppercase-wide text-hud-cyan font-bold">${editing.command ? "Sửa mẫu" : "Tạo mẫu mới"}</div>
                                <div class="text-[10px] text-hud-muted">Gõ lệnh bằng dấu /, ví dụ /gia hoặc /ship.</div>
                            </div>
                            <button id="fb-slash-new" class="border border-hud-cyan/25 bg-hud-cyan/10 px-3 py-2 text-[10px] uppercase-wide font-bold text-hud-cyan hover:border-hud-cyan" type="button"><i class="fa-solid fa-plus"></i> Tạo mới</button>
                        </div>
                        <div>
                            <label class="block text-[10px] uppercase-wide text-hud-fb mb-1">Lệnh</label>
                            <input id="fb-slash-command" class="hud-input w-full px-3 py-2 text-xs" value="${escapeHtml(editing.command || "")}" placeholder="/gia">
                        </div>
                        <div>
                            <label class="block text-[10px] uppercase-wide text-hud-fb mb-1">Tên hiển thị</label>
                            <input id="fb-slash-label" class="hud-input w-full px-3 py-2 text-xs" value="${escapeHtml(editing.label || "")}" placeholder="Hỏi giá">
                        </div>
                        <div>
                            <label class="block text-[10px] uppercase-wide text-hud-fb mb-1">Nội dung</label>
                            <textarea id="fb-slash-text" class="hud-input w-full px-3 py-2 text-xs min-h-[190px]" placeholder="Nội dung trả lời nhanh...">${escapeHtml(editing.text || "")}</textarea>
                        </div>
                        <div class="flex justify-end gap-2 pt-2">
                            <button class="border border-hud-fb/35 bg-hud-fb/15 px-5 py-2.5 text-[10px] uppercase-wide font-bold text-hud-fb hover:border-hud-fb" type="submit"><i class="fa-solid fa-floppy-disk"></i> Lưu mẫu</button>
                        </div>
                    </form>
                </div>
            </div>
        </div>`;
    }

    function openFacebookSlashManager(command = "") {
        state.facebookSlashEditingCommand = command;
        document.getElementById("fb-slash-dialog")?.remove();
        document.body.insertAdjacentHTML("beforeend", renderFacebookSlashManagerDialog());
    }

    function closeFacebookSlashManager() {
        document.getElementById("fb-slash-dialog")?.remove();
        state.facebookSlashEditingCommand = "";
    }

    async function deleteFacebookSlashCommand(command, reopenManager = true) {
        persistFacebookSlashCommands(facebookSlashCommands().filter((item) => item.command !== command));
        await removeFacebookSlashCommand(command);
        if (reopenManager) openFacebookSlashManager();
    }

    function renderFacebookConversationPanel(selected, detailLoading = false) {
        if (!selected) {
            return `<div class="p-10 text-center text-hud-muted">Chọn một hội thoại để xem tin nhắn.</div>`;
        }
        const messages = selected.messages || [];
        const draftMedia = (state.facebookMessageDraftMedia || []).filter((item) => item.conversation_id === selected.conversation_id);
        return `
            <div class="header-strip px-4 py-3 flex items-center gap-3" style="background: linear-gradient(90deg, rgba(74, 158, 255, 0.15) 0%, rgba(74, 158, 255, 0.02) 50%, rgba(74, 158, 255, 0.15) 100%); border-bottom-color: rgba(74, 158, 255, 0.4);">
                <div class="w-8 h-8 rounded-full bg-hud-fb/20 border border-hud-fb flex items-center justify-center flex-shrink-0">
                    <i class="fa-solid fa-user text-hud-fb text-[10px]"></i>
                </div>
                <div class="min-w-0">
                    <div class="text-sm text-white font-bold truncate">${escapeHtml(selected.customer_name || "Facebook User")}</div>
                    <div class="text-[9px] uppercase-wide" style="color:#4a9eff;">${escapeHtml(selected.page_name || "Facebook Page")} · ${escapeHtml(selected.customer_id || "")}</div>
                </div>
                <div class="ml-auto flex gap-2 items-center">
                    <span class="badge ${Number(selected.unread_count || 0) ? "amber" : "cyan"}">${Number(selected.unread_count || 0) ? `${formatNumber(selected.unread_count)} UNREAD` : "OPEN"}</span>
                    <button class="btn-ghost px-2.5 py-1.5 text-[10px]" title="Gắn thẻ"><i class="fa-solid fa-user-tag"></i> TAG</button>
                    <button class="btn-ghost px-2.5 py-1.5 text-[10px]" title="Báo cáo"><i class="fa-solid fa-flag"></i></button>
                </div>
            </div>
            <div id="fb-message-thread" class="flex-1 min-h-0 overflow-y-auto p-5 space-y-3">
                ${detailLoading ? `<div class="text-[10px] text-hud-fb uppercase-wide mb-3"><i class="fa-solid fa-spinner fa-spin"></i> Đang tải thêm tin nhắn trong hội thoại...</div>` : ""}
                ${messages.map((message) => renderFacebookMessageRow(message)).join("") || `<div class="fb-message-empty text-center text-hud-muted text-sm py-10">Hội thoại chưa có tin nhắn text.</div>`}
            </div>
            <div class="border-t border-hud-fb/20 p-3 space-y-2">
                <div id="fb-message-feedback" class="hidden text-[11px] border p-2"></div>
                ${renderFacebookSlashMenu("")}
                <div id="fb-message-media-preview" class="${draftMedia.length ? "" : "hidden"} border border-hud-fb/30 bg-hud-fb/10 px-3 py-2 text-[11px] text-white/90 space-y-2">
                    ${draftMedia.map((item) => `
                        <div class="flex items-center gap-3" data-media-id="${escapeHtml(item.media_id || "")}">
                            <i class="fa-solid ${item.type === "image" ? "fa-image" : item.type === "video" ? "fa-video" : item.type === "audio" ? "fa-microphone" : "fa-paperclip"} text-hud-fb"></i>
                            <span class="min-w-0 flex-1">
                                <span class="block font-bold truncate">${escapeHtml(item.name || "Media")}</span>
                                <span class="block text-[9px] text-hud-muted uppercase-wide">${escapeHtml(item.type || "")} · ${formatNumber(item.size || 0)} bytes</span>
                            </span>
                            <button class="fb-message-media-remove btn-ghost px-2 py-1 text-[10px]" data-media-id="${escapeHtml(item.media_id || "")}" title="Bỏ media"><i class="fa-solid fa-xmark"></i></button>
                        </div>
                    `).join("")}
                </div>
                <div class="flex gap-2">
                    <input id="fb-message-input" type="text" placeholder="Gõ phản hồi của bạn..." class="hud-input flex-1 px-3 py-2 text-xs"/>
                    <input id="fb-message-media-input" type="file" class="hidden" accept="image/*,video/mp4,audio/*,application/pdf" multiple/>
                    <button id="fb-message-media-button" class="btn-ghost px-3 py-2 text-[10px]" title="Đính kèm"><i class="fa-solid fa-paperclip"></i></button>
                    <button class="btn-ghost px-3 py-2 text-[10px] uppercase-wide font-bold" style="background: rgba(0, 240, 255, 0.15); color: #00f0ff;"><i class="fa-solid fa-robot"></i> AI</button>
                    <button id="fb-message-send" class="px-4 py-2 text-xs uppercase-wide font-bold" style="background:#4a9eff;color:#fff;border:1px solid #4a9eff;"><i class="fa-solid fa-paper-plane"></i></button>
                </div>
            </div>
        `;
    }

    function renderFacebookMessageRow(message) {
        const localStatus = String(message.local_status || "");
        const statusLabel = localStatus === "sending" ? " · Đang gửi" : localStatus === "failed" ? " · Lỗi gửi" : "";
        return `
            <div class="fb-message-row flex gap-2 max-w-[80%] ${message.direction === "outbound" ? "ml-auto flex-row-reverse" : ""} ${localStatus === "sending" ? "opacity-70" : ""}" data-message-id="${escapeHtml(message.message_id || "")}">
                <div class="w-7 h-7 rounded-full ${message.direction === "outbound" ? "bg-hud-fb/30 border-hud-fb" : "bg-black/50 border-hud-cyan/30"} border flex items-center justify-center flex-shrink-0 mt-0.5">
                    <i class="fa-solid ${localStatus === "sending" ? "fa-spinner fa-spin text-hud-fb" : message.direction === "outbound" ? "fa-robot text-hud-fb" : "fa-user text-hud-muted"} text-[9px]"></i>
                </div>
                <div>
                    ${renderReplyMessage(message)}
                    <div class="text-[9px] ${localStatus === "failed" ? "text-hud-red" : "text-hud-muted"} mt-1 px-1 ${message.direction === "outbound" ? "text-right" : ""}">${escapeHtml(formatDate(message.created_time))} · ${escapeHtml(message.from_name || "")}${escapeHtml(statusLabel)}</div>
                </div>
            </div>
        `;
    }

    function renderFacebookConversationItem(conversation, selectedConversationId = "") {
        const active = selectedConversationId && selectedConversationId === conversation.conversation_id;
        const hasUnread = Number(conversation.unread_count || 0) > 0;
        return `
            <button class="fb-conversation-item block w-full text-left p-3 border-b border-hud-fb/10 hover:bg-hud-fb/5 ${active ? "bg-hud-fb/10" : ""}" data-conversation-id="${escapeHtml(conversation.conversation_id)}">
                <div class="flex items-start gap-3">
                    <div class="w-10 h-10 rounded-full ${hasUnread ? "bg-hud-fb/20 border-hud-fb" : "bg-black/50 border-hud-cyan/30"} border flex items-center justify-center flex-shrink-0">
                        <i class="fa-solid fa-user ${hasUnread ? "text-hud-fb" : "text-hud-muted"} text-xs"></i>
                    </div>
                    <div class="flex-1 min-w-0">
                        <div class="flex items-center gap-1 mb-0.5">
                            <span class="text-xs font-bold text-white truncate">${escapeHtml(conversation.customer_name || "Facebook User")}</span>
                            <span class="fb-conversation-time text-[9px] text-hud-muted ml-auto">${escapeHtml(formatDate(conversation.updated_time))}</span>
                        </div>
                        <div class="fb-conversation-snippet text-[10px] text-white/70 truncate">${escapeHtml(conversation.snippet || "Không có nội dung hiển thị")}</div>
                        <div class="flex items-center gap-1 mt-1.5">
                            <span class="fb-conversation-state badge ${hasUnread ? "amber" : "cyan"}" style="font-size:8px;">${hasUnread ? `${formatNumber(conversation.unread_count)} UNREAD` : "OPEN"}</span>
                            <span class="fb-conversation-meta text-[8px] text-hud-muted uppercase-wide ml-1">${escapeHtml(conversation.page_name || "Page")} · ${formatNumber(conversation.message_count || 0)} msgs</span>
                        </div>
                    </div>
                </div>
            </button>
        `;
    }

    function upsertFacebookConversationState(conversation, message) {
        const conversationId = conversation?.conversation_id || message?.conversation_id || "";
        if (!conversationId) return;
        const existing = state.facebookConversations.find((item) => item.conversation_id === conversationId) || {};
        const next = {
            ...existing,
            ...conversation,
            conversation_id: conversationId,
            messages: conversation?.messages?.length ? conversation.messages : [message].filter(Boolean),
        };
        if (message?.message_id) {
            next.snippet = message.message || message.fallback_label || next.snippet || "";
            next.updated_time = message.created_time || next.updated_time || "";
            next.message_count = Math.max(Number(next.message_count || 0), Number(existing.message_count || 0) + 1);
        }
        state.facebookConversations = [
            next,
            ...state.facebookConversations.filter((item) => item.conversation_id !== conversationId),
        ].sort((a, b) => String(b.updated_time || "").localeCompare(String(a.updated_time || "")));
        updateFacebookUnreadBadges();
    }

    function appendRealtimeFacebookMessage(message) {
        if (!message?.message_id || message.conversation_id !== state.selectedFacebookConversationId) return;
        const thread = document.getElementById("fb-message-thread");
        if (!thread) return;
        const selector = `[data-message-id="${CSS.escape(message.message_id)}"]`;
        if (thread.querySelector(selector)) return;
        thread.querySelector(".fb-message-empty")?.remove();
        thread.insertAdjacentHTML("beforeend", renderFacebookMessageRow(message));
        scrollFacebookMessagesToBottom(document.getElementById("page-fb-messages"));
    }

    function removeFacebookMessageFromDetail(conversationId, messageId) {
        const detail = state.facebookConversationDetails[conversationId];
        if (detail?.messages?.length) {
            detail.messages = detail.messages.filter((item) => item.message_id !== messageId);
        }
    }

    function findMatchingOptimisticFacebookMessage(conversationId, message) {
        const detail = state.facebookConversationDetails[conversationId];
        const messages = detail?.messages || [];
        return messages.find((item) => {
            return item.local_status === "sending"
                && item.direction === message.direction
                && String(item.message || "") === String(message.message || "");
        }) || null;
    }

    function removeOptimisticFacebookMessage(conversationId, message) {
        const optimistic = findMatchingOptimisticFacebookMessage(conversationId, message);
        if (!optimistic?.message_id) return;
        removeFacebookMessageFromDetail(conversationId, optimistic.message_id);
        document
            .querySelector(`#fb-message-thread [data-message-id="${CSS.escape(optimistic.message_id)}"]`)
            ?.remove();
    }

    function createOptimisticFacebookMessage(conversationId, text, mediaItems = []) {
        const conversation = state.facebookConversations.find((item) => item.conversation_id === conversationId) || {};
        const attachments = (Array.isArray(mediaItems) ? mediaItems : [mediaItems]).filter((item) => item?.url);
        return {
            message_id: `local-${Date.now()}-${Math.random().toString(16).slice(2)}`,
            conversation_id: conversationId,
            page_id: conversation.page_id || "",
            customer_id: conversation.customer_id || "",
            message: text,
            created_time: new Date().toISOString(),
            from_id: conversation.page_id || "",
            from_name: conversation.page_name || "Page",
            to_id: conversation.customer_id || "",
            to_name: conversation.customer_name || "Facebook User",
            direction: "outbound",
            attachments: attachments.map((media) => ({
                attachment_id: media.media_id || "",
                type: media.type || "file",
                mime_type: media.mime_type || "",
                name: media.name || "",
                url: media.url || "",
                preview_url: media.type === "image" ? media.url || "" : "",
                size: media.size || 0,
            })),
            fallback_label: attachments.length && !text ? "Đã gửi tệp đính kèm" : "",
            reply_to: {},
            local_status: "sending",
        };
    }

    function markOptimisticFacebookMessageFailed(conversationId, messageId) {
        const detail = state.facebookConversationDetails[conversationId];
        const target = (detail?.messages || []).find((item) => item.message_id === messageId);
        if (target) target.local_status = "failed";
        const row = document.querySelector(`#fb-message-thread [data-message-id="${CSS.escape(messageId)}"]`);
        if (row && target) {
            row.outerHTML = renderFacebookMessageRow(target);
        }
    }

    function appendOptimisticFacebookMessage(message) {
        const conversationId = message.conversation_id || "";
        if (!conversationId) return;
        const existingDetail = state.facebookConversationDetails[conversationId];
        if (existingDetail) {
            existingDetail.messages = [...(existingDetail.messages || []), message];
            existingDetail.snippet = message.message || existingDetail.snippet || "";
            existingDetail.updated_time = message.created_time || existingDetail.updated_time || "";
        } else {
            const summary = state.facebookConversations.find((item) => item.conversation_id === conversationId);
            if (summary) {
                state.facebookConversationDetails[conversationId] = {
                    ...summary,
                    messages: [...(summary.messages || []), message],
                    snippet: message.message || summary.snippet || "",
                    updated_time: message.created_time || summary.updated_time || "",
                };
            }
        }
        upsertFacebookConversationState({ conversation_id: conversationId }, message);
        appendRealtimeFacebookMessage(message);
        updateRealtimeFacebookConversationList(conversationId);
    }

    function facebookComposerIsFocused(conversationId = "") {
        const active = document.activeElement;
        return Boolean(
            active
            && active.id === "fb-message-input"
            && active.closest("#fb-conversation-panel")
            && (!conversationId || state.selectedFacebookConversationId === conversationId)
        );
    }

    function markFacebookConversationReadLocal(conversationId, persist = true) {
        const conversation = state.facebookConversations.find((item) => item.conversation_id === conversationId);
        if (!conversation) return;
        conversation.unread_count = 0;
        conversation.status = "open";
        const detail = state.facebookConversationDetails[conversationId];
        if (detail) {
            detail.unread_count = 0;
            detail.status = "open";
        }
        updateRealtimeFacebookConversationList(conversationId);
        const panel = document.querySelector("#fb-conversation-panel");
        if (panel && state.selectedFacebookConversationId === conversationId && !facebookComposerIsFocused(conversationId)) {
            const selected = detail || conversation;
            panel.innerHTML = `<span class="c-tl" style="border-color:#4a9eff;"></span><span class="c-br" style="border-color:#4a9eff;"></span>${renderFacebookConversationPanel(selected, false)}`;
            scrollFacebookMessagesToBottom(document.getElementById("page-fb-messages"));
        }
        if (persist) {
            fetchJSON(`/facebook/conversations/${encodeURIComponent(conversationId)}/read`, { method: "POST" })
                .catch((error) => console.warn("Facebook mark read failed", error));
        }
        updateFacebookUnreadBadges();
    }

    function facebookUnreadTotal() {
        return state.facebookConversations.reduce((sum, item) => sum + Number(item.unread_count || 0), 0);
    }

    function updateFacebookUnreadBadges() {
        const total = facebookUnreadTotal();
        const sidebar = document.getElementById("fb-messages-sidebar-unread");
        if (sidebar) {
            sidebar.textContent = formatNumber(total);
            sidebar.classList.toggle("hidden", total <= 0);
            sidebar.classList.toggle("bg-hud-red/20", total > 0);
            sidebar.classList.toggle("text-hud-red", total > 0);
            sidebar.classList.toggle("blink", total > 0);
            sidebar.classList.toggle("bg-hud-fb/20", total <= 0);
            sidebar.classList.toggle("text-hud-fb", total <= 0);
        }
        const header = document.getElementById("fb-messages-header-unread");
        if (header) header.textContent = `INBOX · ${formatNumber(total)} UNREAD`;
    }

    async function uploadFacebookMessageMedia(file, conversationId) {
        const formData = new FormData();
        formData.append("file", file);
        const result = await fetchJSON("/facebook/messages/media", {
            method: "POST",
            body: formData,
        });
        return { ...result, conversation_id: conversationId };
    }

    function updateRealtimeFacebookConversationList(conversationId) {
        const conversation = state.facebookConversations.find((item) => item.conversation_id === conversationId);
        const list = document.getElementById("fb-conversation-list");
        if (!conversation || !list) return;
        list.querySelector(".fb-conversation-empty")?.remove();
        const existing = list.querySelector(`.fb-conversation-item[data-conversation-id="${CSS.escape(conversationId)}"]`);
        existing?.remove();
        const html = renderFacebookConversationItem(conversation, state.selectedFacebookConversationId);
        const nextSibling = Array.from(list.querySelectorAll(".fb-conversation-item")).find((item) => {
            const other = state.facebookConversations.find((entry) => entry.conversation_id === item.dataset.conversationId);
            return String(other?.updated_time || "").localeCompare(String(conversation.updated_time || "")) < 0;
        });
        if (nextSibling) {
            nextSibling.insertAdjacentHTML("beforebegin", html);
        } else {
            list.insertAdjacentHTML("beforeend", html);
        }
        const inserted = list.querySelector(`.fb-conversation-item[data-conversation-id="${CSS.escape(conversationId)}"]`);
        if (inserted) bindFacebookConversationButton(inserted);
        list.querySelectorAll(".fb-conversation-item").forEach((item) => {
            item.classList.toggle("bg-hud-fb/10", item.dataset.conversationId === state.selectedFacebookConversationId);
        });
    }

    function patchFacebookConversationFromSummary(conversation) {
        if (!conversation?.conversation_id) return;
        const messages = conversation.messages || [];
        if (!state.selectedFacebookConversationId) {
            state.selectedFacebookConversationId = conversation.conversation_id;
        }
        upsertFacebookConversationState(conversation, messages[messages.length - 1] || {});
        updateRealtimeFacebookConversationList(conversation.conversation_id);
        const panel = document.querySelector("#fb-conversation-panel");
        if (
            panel
            && state.selectedFacebookConversationId === conversation.conversation_id
            && !state.facebookConversationDetails[conversation.conversation_id]
            && !facebookComposerIsFocused(conversation.conversation_id)
        ) {
            panel.innerHTML = `<span class="c-tl" style="border-color:#4a9eff;"></span><span class="c-br" style="border-color:#4a9eff;"></span>${renderFacebookConversationPanel(conversation, false)}`;
            scrollFacebookMessagesToBottom(document.getElementById("page-fb-messages"));
        }
    }

    async function patchFacebookConversationsFromCache(conversations) {
        for (const conversation of conversations || []) {
            patchFacebookConversationFromSummary(conversation);
            await refreshFacebookSelectedConversationIfNeeded(conversation);
        }
    }

    async function startFacebookConversationSyncJob(limit = 25) {
        if (state.facebookMessagesSyncJobId) return state.facebookMessagesSyncJobId;
        const result = await fetchJSON(`/facebook/conversations/sync?limit=${limit}`, { method: "POST" });
        const jobId = result?.job?.job_id || "";
        state.facebookMessagesSyncJobId = jobId;
        state.facebookMessagesSyncing = Boolean(jobId);
        return jobId;
    }

    async function pollFacebookConversationSyncJob() {
        const jobId = state.facebookMessagesSyncJobId;
        if (!jobId) return false;
        const result = await fetchJSON(`/facebook/conversations/sync/${encodeURIComponent(jobId)}`);
        const job = result?.job || {};
        if (!["completed", "failed"].includes(job.status)) return false;
        state.facebookMessagesSyncJobId = "";
        state.facebookMessagesSyncing = false;
        if (job.status === "completed") {
            const [payload] = await Promise.all([
                fetchJSON("/facebook/conversations?limit=25&message_limit=1"),
                loadFacebookSlashCommands().catch((error) => {
                    console.warn("Facebook slash commands load failed", error);
                    return [];
                }),
            ]);
            await patchFacebookConversationsFromCache(payload.conversations || []);
        }
        return true;
    }

    async function applyFacebookConversationSyncCompleted(payload) {
        state.facebookMessagesSyncJobId = "";
        state.facebookMessagesSyncing = false;
        await patchFacebookConversationsFromCache(payload?.conversations || []);
        document.querySelector("#fb-messages-sync-status")?.classList.add("hidden");
        const button = document.querySelector("#fb-messages-refresh");
        if (button) button.innerHTML = `<i class="fa-solid fa-rotate"></i>`;
    }

    async function refreshFacebookSelectedConversationIfNeeded(conversation) {
        const conversationId = conversation?.conversation_id || "";
        if (!conversationId || conversationId !== state.selectedFacebookConversationId) return;
        const detail = state.facebookConversationDetails[conversationId];
        const summaryMessages = conversation.messages || [];
        const latestSummaryMessage = summaryMessages[summaryMessages.length - 1] || {};
        const existingLatest = (detail?.messages || []).slice(-1)[0] || {};
        if (
            detail
            && latestSummaryMessage.message_id
            && existingLatest.message_id === latestSummaryMessage.message_id
        ) {
            return;
        }
        if (state.facebookConversationDetailPending[conversationId]) return;
        state.facebookConversationDetailPending[conversationId] = true;
        try {
            const loadedDetail = await fetchJSON(`/facebook/conversations/${encodeURIComponent(conversationId)}?message_limit=100`);
            state.facebookConversationDetails[conversationId] = loadedDetail;
            const panel = document.querySelector("#fb-conversation-panel");
            if (state.selectedFacebookConversationId === conversationId && panel && !facebookComposerIsFocused(conversationId)) {
                panel.innerHTML = `<span class="c-tl" style="border-color:#4a9eff;"></span><span class="c-br" style="border-color:#4a9eff;"></span>${renderFacebookConversationPanel(loadedDetail, false)}`;
                scrollFacebookMessagesToBottom(document.getElementById("page-fb-messages"));
            }
        } catch (error) {
            console.warn("Facebook conversation fallback detail failed", error);
        } finally {
            delete state.facebookConversationDetailPending[conversationId];
        }
    }

    async function runFacebookMessagesFallbackSync() {
        if (!state.facebookMessagesFallbackActive) return;
        if (state.facebookMessagesSyncing && !state.facebookMessagesSyncJobId) return;
        try {
            if (state.facebookMessagesSyncJobId) {
                await pollFacebookConversationSyncJob();
                return;
            }
            await startFacebookConversationSyncJob(25);
        } catch (error) {
            console.warn("Facebook messages fallback sync failed", error);
            state.facebookMessagesSyncJobId = "";
            state.facebookMessagesSyncing = false;
        }
    }

    function startFacebookMessagesFallbackSync() {
        state.facebookMessagesFallbackActive = true;
        if (state.facebookMessagesFallbackTimer) return;
        state.facebookMessagesFallbackTimer = setInterval(runFacebookMessagesFallbackSync, 12000);
    }

    function stopFacebookMessagesFallbackSync() {
        state.facebookMessagesFallbackActive = false;
        if (state.facebookMessagesFallbackTimer) {
            clearInterval(state.facebookMessagesFallbackTimer);
            state.facebookMessagesFallbackTimer = null;
        }
    }

    function bindFacebookConversationButton(button) {
        if (!button || button.dataset.bound === "1") return;
        button.dataset.bound = "1";
        button.addEventListener("click", () => {
            const section = document.getElementById("page-fb-messages");
            if (!section) return;
            state.selectedFacebookConversationId = button.dataset.conversationId || "";
            section.querySelectorAll(".fb-conversation-item").forEach((item) => item.classList.remove("bg-hud-fb/10"));
            button.classList.add("bg-hud-fb/10");
            const conversationId = state.selectedFacebookConversationId;
            markFacebookConversationReadLocal(conversationId);
            const summary = state.facebookConversations.find((item) => item.conversation_id === conversationId) || null;
            const detail = summary ? state.facebookConversationDetails[summary.conversation_id] : null;
            const panel = section.querySelector("#fb-conversation-panel");
            if (panel) {
                panel.innerHTML = `<span class="c-tl" style="border-color:#4a9eff;"></span><span class="c-br" style="border-color:#4a9eff;"></span>${renderFacebookConversationPanel(detail || summary, Boolean(summary && !detail))}`;
                scrollFacebookMessagesToBottom(section);
            }
            if (summary && !detail && !state.facebookConversationDetailPending[conversationId]) {
                state.facebookConversationDetailPending[conversationId] = true;
                fetchJSON(`/facebook/conversations/${encodeURIComponent(conversationId)}?message_limit=100`)
                    .then((loadedDetail) => {
                        state.facebookConversationDetails[conversationId] = loadedDetail;
                        if (state.selectedFacebookConversationId === conversationId) {
                            const currentPanel = section.querySelector("#fb-conversation-panel");
                            if (currentPanel && !facebookComposerIsFocused(conversationId)) {
                                currentPanel.innerHTML = `<span class="c-tl" style="border-color:#4a9eff;"></span><span class="c-br" style="border-color:#4a9eff;"></span>${renderFacebookConversationPanel(loadedDetail, false)}`;
                                scrollFacebookMessagesToBottom(section);
                            }
                        }
                    })
                    .catch((error) => console.warn("Facebook conversation detail failed", error))
                    .finally(() => {
                        delete state.facebookConversationDetailPending[conversationId];
                    });
            }
        });
    }

    function applyFacebookMessageRealtime(payload) {
        if (payload?.type === "facebook.conversation.synced") {
            const conversation = payload?.conversation || {};
            patchFacebookConversationFromSummary(conversation);
            if (conversation?.conversation_id === state.selectedFacebookConversationId) {
                markFacebookConversationReadLocal(conversation.conversation_id);
            }
            refreshFacebookSelectedConversationIfNeeded(conversation).catch((error) => console.warn("Facebook synced conversation refresh failed", error));
            return;
        }
        if (payload?.type === "facebook.conversations.sync.completed") {
            applyFacebookConversationSyncCompleted(payload).catch((error) => console.warn("Facebook sync completed handler failed", error));
            return;
        }
        if (payload?.type === "facebook.conversations.sync.failed") {
            state.facebookMessagesSyncJobId = "";
            state.facebookMessagesSyncing = false;
            return;
        }
        const conversation = payload?.conversation || {};
        const message = payload?.message || {};
        const conversationId = payload?.conversation_id || conversation.conversation_id || message.conversation_id || "";
        if (!conversationId) return;
        const normalizedMessage = { ...message, conversation_id: conversationId };
        if (normalizedMessage.direction === "outbound" && !normalizedMessage.local_status) {
            removeOptimisticFacebookMessage(conversationId, normalizedMessage);
        }
        const normalizedConversation = { ...conversation, conversation_id: conversationId };
        if ((normalizedConversation.messages || []).some((item) => item.local_status)) {
            normalizedConversation.messages = normalizedConversation.messages.filter((item) => !item.local_status);
        }
        upsertFacebookConversationState(normalizedConversation, normalizedMessage);
        const detail = state.facebookConversationDetails[conversationId];
        if (detail) {
            const messages = detail.messages || [];
            if (normalizedMessage.message_id && !messages.some((item) => item.message_id === normalizedMessage.message_id)) {
                detail.messages = [...messages, normalizedMessage];
            }
            detail.snippet = normalizedMessage.message || normalizedMessage.fallback_label || detail.snippet || "";
            detail.updated_time = normalizedMessage.created_time || detail.updated_time || "";
        }
        appendRealtimeFacebookMessage(normalizedMessage);
        updateRealtimeFacebookConversationList(conversationId);
        if (normalizedMessage.direction === "inbound" && conversationId === state.selectedFacebookConversationId) {
            markFacebookConversationReadLocal(conversationId);
        }
    }

    function closeFacebookMessagesStream() {
        stopFacebookMessagesFallbackSync();
        if (state.facebookMessagesSocketReconnectTimer) {
            clearTimeout(state.facebookMessagesSocketReconnectTimer);
            state.facebookMessagesSocketReconnectTimer = null;
        }
        state.facebookMessagesStreamActive = false;
        if (state.facebookMessagesSocket) {
            state.facebookMessagesSocket.onclose = null;
            state.facebookMessagesSocket.close();
            state.facebookMessagesSocket = null;
        }
    }

    function connectFacebookMessagesStream() {
        if (state.facebookMessagesSocket && state.facebookMessagesSocket.readyState <= 1) return;
        state.facebookMessagesStreamActive = true;
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        state.facebookMessagesSocket = new WebSocket(`${protocol}//${window.location.host}${API_BASE}/realtime/ws`);
        state.facebookMessagesSocket.onopen = () => {
            state.facebookMessagesReconnectAttempts = 0;
            state.facebookMessagesSocket?.send(JSON.stringify({ type: "subscribe", channels: ["facebook:messages"] }));
        };
        state.facebookMessagesSocket.onmessage = (event) => {
            let data = {};
            try {
                data = JSON.parse(event.data);
            } catch {
                return;
            }
            if (data.channel === "facebook:messages") applyFacebookMessageRealtime(data.payload || {});
        };
        state.facebookMessagesSocket.onclose = () => {
            state.facebookMessagesSocket = null;
            if (!state.facebookMessagesStreamActive) return;
            const delay = Math.min(1000 * (state.facebookMessagesReconnectAttempts + 1), 8000);
            state.facebookMessagesReconnectAttempts += 1;
            state.facebookMessagesSocketReconnectTimer = setTimeout(connectFacebookMessagesStream, delay);
        };
    }

    async function renderFacebookMessagesPage() {
        const section = document.getElementById("page-fb-messages");
        if (!section) return;
        if (!section.dataset.hydrated) {
            section.innerHTML = `<div class="max-w-7xl mx-auto text-hud-muted text-sm">Đang đọc cache tin nhắn...</div>`;
        }
        try {
            const payload = await fetchJSON("/facebook/conversations?limit=25&message_limit=1");
            const conversations = payload.conversations || [];
            state.facebookConversations = conversations;
            updateFacebookUnreadBadges();
            connectFacebookMessagesStream();
            startFacebookMessagesFallbackSync();
            if (!state.selectedFacebookConversationId && conversations[0]) state.selectedFacebookConversationId = conversations[0].conversation_id;
            const selectedSummary = conversations.find((item) => item.conversation_id === state.selectedFacebookConversationId) || conversations[0] || null;
            const selectedDetail = selectedSummary?.conversation_id ? state.facebookConversationDetails[selectedSummary.conversation_id] : null;
            const selected = selectedDetail || selectedSummary;
            const detailLoading = Boolean(selectedSummary?.conversation_id && !selectedDetail);
            const unread = conversations.reduce((sum, item) => sum + Number(item.unread_count || 0), 0);
            section.dataset.hydrated = "1";
            section.innerHTML = `
                <div class="max-w-7xl mx-auto flex flex-col min-h-0" style="height: calc(100dvh - 120px);">
                    <div id="fb-messages-sync-status" class="${state.facebookMessagesSyncing ? "" : "hidden"} mb-4 border border-hud-cyan/30 bg-hud-cyan/10 text-hud-cyan text-[11px] p-3">
                        Đang đồng bộ inbox Facebook trong nền. Tin nhắn đã lưu vẫn hiển thị, sync xong sẽ tự cập nhật.
                    </div>
                    ${facebookWarningBanner(payload.warnings)}
                    <div id="fb-messages-grid" class="grid gap-4 flex-1 min-h-0" style="grid-template-columns: 320px minmax(0, 1fr); transition: grid-template-columns 0.3s ease;">
                        <div class="hud-card flex flex-col overflow-hidden fade-in min-h-0" style="border-color: rgba(74, 158, 255, 0.3);">
                            <span class="c-tl" style="border-color:#4a9eff;"></span><span class="c-br" style="border-color:#4a9eff;"></span>
                            <div class="header-strip px-4 py-3 flex items-center gap-2" style="background: linear-gradient(90deg, rgba(74, 158, 255, 0.15) 0%, rgba(74, 158, 255, 0.02) 50%, rgba(74, 158, 255, 0.15) 100%); border-bottom-color: rgba(74, 158, 255, 0.4);">
                                <i class="fa-solid fa-inbox text-hud-fb"></i>
                                <span id="fb-messages-header-unread" class="font-display font-black text-[10px] text-white uppercase-widest">INBOX · ${formatNumber(unread)} UNREAD</span>
                                <button id="fb-messages-refresh" class="ml-auto btn-ghost px-3 py-1.5 text-[10px] uppercase-wide font-bold"><i class="fa-solid fa-rotate"></i></button>
                            </div>
                            <div id="fb-conversation-list" class="flex-1 min-h-0 overflow-y-auto">
                                ${conversations.map((conversation) => {
                                    return renderFacebookConversationItem(conversation, selected?.conversation_id || "");
                                }).join("") || `<div class="fb-conversation-empty p-6 text-center text-hud-muted text-sm">Chưa có hội thoại trong DB. Hệ thống sẽ sync nền nếu page token có quyền inbox.</div>`}
                            </div>
                        </div>
                        <div id="fb-conversation-panel" class="hud-card flex flex-col overflow-hidden fade-in min-h-0" style="border-color: rgba(74, 158, 255, 0.3);">
                            <span class="c-tl" style="border-color:#4a9eff;"></span><span class="c-br" style="border-color:#4a9eff;"></span>
                            ${renderFacebookConversationPanel(selected, detailLoading)}
                        </div>
                    </div>
                </div>
            `;
            scrollFacebookMessagesToBottom(section);
            if (selected?.conversation_id) {
                markFacebookConversationReadLocal(selected.conversation_id);
            }
            const syncMessages = async () => {
                if (state.facebookMessagesSyncing) return;
                state.facebookMessagesSyncing = true;
                section.querySelector("#fb-messages-sync-status")?.classList.remove("hidden");
                const button = section.querySelector("#fb-messages-refresh");
                if (button) button.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i>`;
                try {
                    const jobId = await startFacebookConversationSyncJob(25);
                    const status = section.querySelector("#fb-messages-sync-status");
                    if (status) status.textContent = jobId
                        ? `Đã xếp hàng sync inbox Facebook (${jobId.slice(0, 8)}). Có thể chuyển trang, worker sẽ xử lý nền.`
                        : "Đã bắt đầu sync inbox Facebook.";
                } finally {
                    if (button) button.innerHTML = `<i class="fa-solid fa-rotate"></i>`;
                }
            };
            section.querySelector("#fb-messages-refresh")?.addEventListener("click", syncMessages);
            if (!conversations.length && !state.facebookMessagesAutoSynced) {
                state.facebookMessagesAutoSynced = true;
                setTimeout(syncMessages, 50);
            }
            section.querySelectorAll(".fb-conversation-item").forEach((button) => {
                bindFacebookConversationButton(button);
            });
            if (selectedSummary?.conversation_id && !selectedDetail && !state.facebookConversationDetailPending[selectedSummary.conversation_id]) {
                const conversationId = selectedSummary.conversation_id;
                state.facebookConversationDetailPending[conversationId] = true;
                fetchJSON(`/facebook/conversations/${encodeURIComponent(conversationId)}?message_limit=100`)
                    .then((detail) => {
                        state.facebookConversationDetails[conversationId] = detail;
                        if (state.selectedFacebookConversationId === conversationId) {
                            const panel = section.querySelector("#fb-conversation-panel");
                            if (panel && !facebookComposerIsFocused(conversationId)) {
                                panel.innerHTML = `<span class="c-tl" style="border-color:#4a9eff;"></span><span class="c-br" style="border-color:#4a9eff;"></span>${renderFacebookConversationPanel(detail, false)}`;
                                scrollFacebookMessagesToBottom(section);
                            }
                        }
                    })
                    .catch((error) => console.warn("Facebook conversation detail failed", error))
                    .finally(() => {
                        delete state.facebookConversationDetailPending[conversationId];
                    });
            }
            const sendSelectedMessage = async () => {
                const input = section.querySelector("#fb-message-input");
                const feedback = section.querySelector("#fb-message-feedback");
                const message = String(input?.value || "").trim();
                const selectedConversationId = state.selectedFacebookConversationId;
                const draftMedia = (state.facebookMessageDraftMedia || []).filter((item) => item.conversation_id === selectedConversationId);
                if (!selectedConversationId || (!message && !draftMedia.length)) return;
                const optimisticMessage = createOptimisticFacebookMessage(selectedConversationId, message, draftMedia);
                if (input) input.value = "";
                state.facebookMessageDraftMedia = (state.facebookMessageDraftMedia || []).filter((item) => item.conversation_id !== selectedConversationId);
                section.querySelector("#fb-message-media-preview")?.classList.add("hidden");
                if (feedback) feedback.classList.add("hidden");
                appendOptimisticFacebookMessage(optimisticMessage);
                try {
                    const result = await fetchJSON("/facebook/messages/send", {
                        method: "POST",
                        body: JSON.stringify({
                            conversation_id: selectedConversationId,
                            message,
                            attachments: draftMedia.map((item) => ({
                                media_id: item.media_id || "",
                                url: item.url || "",
                                type: item.type || "file",
                                name: item.name || "",
                                mime_type: item.mime_type || "",
                            })),
                        }),
                    });
                    if (result?.message_id) {
                        applyFacebookMessageRealtime({
                            type: "facebook.message.sent",
                            conversation_id: selectedConversationId,
                            conversation: state.facebookConversations.find((item) => item.conversation_id === selectedConversationId) || {},
                            message: {
                                ...optimisticMessage,
                                message_id: result.message_id,
                                local_status: "",
                            },
                        });
                    }
                    if (!state.facebookMessagesStreamActive) {
                        delete state.facebookConversationDetails[selectedConversationId];
                        await renderFacebookMessagesPage();
                    }
                } catch (error) {
                    markOptimisticFacebookMessageFailed(selectedConversationId, optimisticMessage.message_id);
                    if (feedback) {
                        feedback.className = "text-[11px] border p-2 text-hud-red border-hud-red/30 bg-hud-red/10";
                        feedback.textContent = `Send failed: ${error.message}`;
                        feedback.classList.remove("hidden");
                    }
                }
            };
            if (!section.dataset.facebookComposerBound) {
                section.dataset.facebookComposerBound = "1";
                section.addEventListener("click", (event) => {
                    if (event.target.closest("#fb-message-send")) {
                        sendSelectedMessage();
                    }
                    if (event.target.closest(".fb-slash-manage")) {
                        event.preventDefault();
                        event.stopPropagation();
                        openFacebookSlashManager();
                        return;
                    }
                    const slashEdit = event.target.closest(".fb-slash-edit");
                    if (slashEdit) {
                        event.preventDefault();
                        event.stopPropagation();
                        openFacebookSlashManager(slashEdit.dataset.command || "");
                        return;
                    }
                    const slashDelete = event.target.closest(".fb-slash-delete");
                    if (slashDelete) {
                        event.preventDefault();
                        event.stopPropagation();
                        deleteFacebookSlashCommand(slashDelete.dataset.command || "", false)
                            .catch((error) => console.warn("Facebook slash delete failed", error));
                        updateFacebookSlashMenu(section);
                        return;
                    }
                    const slashItem = event.target.closest(".fb-message-slash-item");
                    if (slashItem) {
                        applyFacebookSlashCommand(section, slashItem.dataset.command || "");
                    }
                    if (event.target.closest("#fb-message-media-button")) {
                        section.querySelector("#fb-message-media-input")?.click();
                    }
                    const removeMediaButton = event.target.closest(".fb-message-media-remove");
                    if (removeMediaButton) {
                        const mediaId = removeMediaButton.dataset.mediaId || "";
                        state.facebookMessageDraftMedia = (state.facebookMessageDraftMedia || []).filter((item) => item.media_id !== mediaId);
                        const panel = section.querySelector("#fb-conversation-panel");
                        const selectedConversationId = state.selectedFacebookConversationId;
                        const selected = state.facebookConversationDetails[selectedConversationId]
                            || state.facebookConversations.find((item) => item.conversation_id === selectedConversationId);
                        if (panel) {
                            panel.innerHTML = `<span class="c-tl" style="border-color:#4a9eff;"></span><span class="c-br" style="border-color:#4a9eff;"></span>${renderFacebookConversationPanel(selected, false)}`;
                            scrollFacebookMessagesToBottom(section);
                        }
                    }
                });
                section.addEventListener("change", async (event) => {
                    if (event.target?.id !== "fb-message-media-input") return;
                    const files = Array.from(event.target.files || []).slice(0, 10);
                    const selectedConversationId = state.selectedFacebookConversationId;
                    if (!files.length || !selectedConversationId) return;
                    const feedback = section.querySelector("#fb-message-feedback");
                    try {
                        if (feedback) {
                            feedback.className = "text-[11px] border p-2 text-hud-fb border-hud-fb/30 bg-hud-fb/10";
                            feedback.textContent = `Đang tải ${formatNumber(files.length)} media lên server...`;
                            feedback.classList.remove("hidden");
                        }
                        const uploaded = [];
                        for (const file of files) {
                            uploaded.push(await uploadFacebookMessageMedia(file, selectedConversationId));
                        }
                        state.facebookMessageDraftMedia = [
                            ...(state.facebookMessageDraftMedia || []).filter((item) => item.conversation_id !== selectedConversationId),
                            ...uploaded,
                        ];
                        if (feedback) feedback.classList.add("hidden");
                        const panel = section.querySelector("#fb-conversation-panel");
                        const selected = state.facebookConversationDetails[selectedConversationId]
                            || state.facebookConversations.find((item) => item.conversation_id === selectedConversationId);
                        if (panel) {
                            panel.innerHTML = `<span class="c-tl" style="border-color:#4a9eff;"></span><span class="c-br" style="border-color:#4a9eff;"></span>${renderFacebookConversationPanel(selected, false)}`;
                            scrollFacebookMessagesToBottom(section);
                        }
                    } catch (error) {
                        if (feedback) {
                            feedback.className = "text-[11px] border p-2 text-hud-red border-hud-red/30 bg-hud-red/10";
                            feedback.textContent = `Upload media failed: ${error.message}`;
                            feedback.classList.remove("hidden");
                        }
                    } finally {
                        event.target.value = "";
                    }
                });
                section.addEventListener("keydown", (event) => {
                    if (event.target?.id === "fb-message-input" && event.key === "Enter" && !event.shiftKey) {
                        event.preventDefault();
                        sendSelectedMessage();
                    }
                });
                section.addEventListener("input", (event) => {
                    if (event.target?.id === "fb-message-input") {
                        updateFacebookSlashMenu(section);
                    }
                });
            }
        } catch (error) {
            section.innerHTML = `<div class="max-w-7xl mx-auto text-hud-red text-sm">Failed to load Facebook messages: ${escapeHtml(error.message)}</div>`;
        }
    }

    document.addEventListener("click", (event) => {
        if (event.target.closest("#fb-schedule-close") || event.target.closest("#fb-schedule-cancel")) {
            closeFacebookScheduleDialog();
            return;
        }
        if (event.target.closest(".fb-slash-manage")) {
            event.preventDefault();
            event.stopPropagation();
            openFacebookSlashManager();
            return;
        }
        if (event.target.closest("#fb-slash-close")) {
            closeFacebookSlashManager();
        }
        const edit = event.target.closest(".fb-slash-dialog-edit");
        if (edit) {
            event.preventDefault();
            openFacebookSlashManager(edit.dataset.command || "");
        }
        const del = event.target.closest(".fb-slash-dialog-delete");
        if (del) {
            event.preventDefault();
            deleteFacebookSlashCommand(del.dataset.command || "")
                .catch((error) => console.warn("Facebook slash delete failed", error));
        }
        if (event.target.closest("#fb-slash-new")) {
            state.facebookSlashEditingCommand = "";
            openFacebookSlashManager();
        }
    });

    document.addEventListener("click", (event) => {
        if (!event.target.closest(".fb-slash-manage")) return;
        event.preventDefault();
        event.stopPropagation();
        openFacebookSlashManager();
    }, true);

    document.addEventListener("submit", async (event) => {
        if (event.target?.id === "fb-schedule-form") {
            event.preventDefault();
            const value = String(document.getElementById("fb-schedule-datetime")?.value || "").trim();
            if (!value) return;
            state.facebookCreateScheduledAt = value;
            const scheduledRadio = document.querySelector('input[name="schedule"][value="scheduled"]');
            if (scheduledRadio) scheduledRadio.checked = true;
            state.facebookCreateScheduleMode = "scheduled";
            closeFacebookScheduleDialog();
            setFacebookCreateFeedback("success", `Đã chọn lịch đăng: ${value.replace("T", " ")}.`);
            return;
        }
        if (event.target?.id !== "fb-slash-form") return;
        event.preventDefault();
        const original = document.getElementById("fb-slash-original")?.value || "";
        let command = String(document.getElementById("fb-slash-command")?.value || "").trim();
        const label = String(document.getElementById("fb-slash-label")?.value || "").trim();
        const text = String(document.getElementById("fb-slash-text")?.value || "").trim();
        if (!command || !label || !text) return;
        if (!command.startsWith("/")) command = `/${command}`;
        try {
            await saveFacebookSlashCommand(command, label, text, original);
            openFacebookSlashManager(command);
            updateFacebookSlashMenu(document.getElementById("page-fb-messages"));
        } catch (error) {
            alert(`Không lưu được slash command: ${error.message}`);
        }
    });

    async function renderFacebookPostsPage() {
        const section = document.getElementById("page-fb-posts");
        if (!section) return;
        if (!section.dataset.hydrated) {
            section.innerHTML = `<div class="max-w-7xl mx-auto text-hud-muted text-sm">Đang đọc cache bài viết...</div>`;
        }
        try {
            const payload = await fetchJSON(`/facebook/posts?limit=${state.facebookPostsLimit}&offset=${state.facebookPostsOffset}`);
            const posts = payload.posts || [];
            const totals = payload.totals || {};
            const currentPage = Math.floor((payload.offset || 0) / (payload.limit || state.facebookPostsLimit)) + 1;
            const totalPages = Math.max(1, Math.ceil((payload.total || 0) / (payload.limit || state.facebookPostsLimit)));
            section.dataset.hydrated = "1";
            section.innerHTML = `
                <div class="max-w-7xl mx-auto">
                    <div id="fb-posts-sync-status" class="${state.facebookPostsSyncing ? "" : "hidden"} mb-4 border border-hud-cyan/30 bg-hud-cyan/10 text-hud-cyan text-[11px] p-3">
                        Đang đồng bộ bài viết Facebook trong nền. Dữ liệu đã lưu sẽ vẫn hiển thị, sync xong sẽ tự cập nhật.
                    </div>
                    <div class="grid grid-cols-6 gap-4 mb-6">
                        ${[
                            ["TOTAL POSTS", formatNumber(payload.total || 0), "white"],
                            ["POSTED 7D", formatNumber(totals.posted_7d || 0), "green"],
                            ["REACH", formatCompact(totals.reach || 0), "white"],
                            ["VIEWS", formatCompact(totals.views || 0), "white"],
                            ["ENGAGEMENT", formatCompact(totals.engagement || 0), "white"],
                            ["ANALYTICS", `${formatNumber(totals.analytics_available || 0)}/${formatNumber(posts.length || 0)}`, "amber"],
                        ].map(([label, value, tone]) => `
                            <div class="hud-card ${tone === "green" ? "green" : tone === "amber" ? "amber" : ""} p-4 fade-in" style="border-color: rgba(74, 158, 255, 0.3);">
                                <span class="c-tl" style="border-color:#4a9eff;"></span><span class="c-br" style="border-color:#4a9eff;"></span>
                                <div class="text-[9px] ${tone === "green" ? "text-hud-green" : tone === "amber" ? "text-hud-amber" : ""} uppercase-widest mb-1" ${tone === "white" ? `style="color:#4a9eff;"` : ""}>${label}</div>
                                <div class="metric-num text-2xl ${tone === "green" ? "text-hud-green" : tone === "amber" ? "text-hud-amber" : "text-white"}">${value}</div>
                                <div class="text-[9px] text-hud-muted">${formatNumber(payload.page_count || 0)} pages</div>
                            </div>
                        `).join("")}
                    </div>
                    ${facebookWarningBanner(payload.warnings)}
                    <div class="hud-card fade-in" style="border-color: rgba(74, 158, 255, 0.3);">
                        <span class="c-tl" style="border-color:#4a9eff;"></span><span class="c-br" style="border-color:#4a9eff;"></span>
                        <div class="header-strip px-5 py-3 flex items-center gap-2" style="background: linear-gradient(90deg, rgba(74, 158, 255, 0.15) 0%, rgba(74, 158, 255, 0.02) 50%, rgba(74, 158, 255, 0.15) 100%); border-bottom-color: rgba(74, 158, 255, 0.4);">
                            <i class="fa-solid fa-newspaper text-hud-fb"></i>
                            <span class="font-display font-black text-xs text-white uppercase-widest">FACEBOOK POSTS · ALL PAGES</span>
                            <span class="ml-auto text-[10px] text-hud-muted uppercase-wide">PAGE ${formatNumber(currentPage)} / ${formatNumber(totalPages)}</span>
                            <button id="fb-posts-refresh" class="ml-auto btn-ghost px-3 py-1.5 text-[10px] uppercase-wide font-bold"><i class="fa-solid fa-rotate"></i> REFRESH</button>
                        </div>
                        <div class="overflow-x-auto">
                            <table class="w-full text-left">
                                <thead>
                                    <tr class="text-[10px] uppercase-widest text-hud-muted border-b border-hud-fb/20">
                                        <th class="px-5 py-3">Bài viết</th>
                                        <th class="px-4 py-3">Page</th>
                                        <th class="px-4 py-3">Thời gian</th>
                                        <th class="px-4 py-3 text-right">Reach</th>
                                        <th class="px-4 py-3 text-right">Views</th>
                                        <th class="px-4 py-3 text-right">Engaged</th>
                                        <th class="px-4 py-3 text-right">Clicks</th>
                                        <th class="px-4 py-3 text-right">Cmt/React/Share</th>
                                        <th class="px-4 py-3">Status</th>
                                        <th class="px-4 py-3 text-right">Actions</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    ${posts.map((post) => {
                                        const analyticsStatus = post.analytics_status || "";
                                        const analyticsAvailable = ["available", "partial", "stale"].includes(analyticsStatus) || Number(post.reach || post.views || post.impressions || post.engagement || post.clicks || post.comments || post.reactions || post.shares || 0) > 0;
                                        const analyticsLabel = analyticsStatus === "partial" ? "PARTIAL" : analyticsStatus === "stale" ? "STALE" : analyticsAvailable ? "ANALYTICS" : analyticsStatus === "error" ? "ERROR" : "NO DATA";
                                        const analyticsTone = analyticsStatus === "partial" || analyticsStatus === "stale" ? "amber" : analyticsAvailable ? "green" : "amber";
                                        return `
                                        <tr class="border-b border-hud-fb/10 hover:bg-hud-fb/5">
                                            <td class="px-5 py-4">
                                                <div class="flex items-start gap-3">
                                                    ${post.full_picture ? `<img src="${escapeHtml(post.full_picture)}" alt="" class="w-12 h-12 object-cover border border-hud-fb/20"/>` : `<div class="w-12 h-12 border border-hud-fb/20 bg-hud-fb/10 flex items-center justify-center"><i class="fa-brands fa-facebook text-hud-fb"></i></div>`}
                                                    <div class="min-w-0">
                                                        <div class="text-sm text-white font-bold truncate max-w-[520px]">${escapeHtml(post.message || "Bài viết không có nội dung text")}</div>
                                                        <div class="text-[10px] text-hud-muted uppercase-wide mt-1">${escapeHtml(post.type || "post")} · ${escapeHtml(post.post_id || "")}</div>
                                                    </div>
                                                </div>
                                            </td>
                                            <td class="px-4 py-4 text-[11px] text-white">${escapeHtml(post.page_name || "Facebook Page")}</td>
                                            <td class="px-4 py-4 text-[11px] text-hud-muted">${escapeHtml(formatDate(post.created_time))}</td>
                                            <td class="px-4 py-4 text-right text-white font-mono">${formatCompact(post.reach || 0)}</td>
                                            <td class="px-4 py-4 text-right text-white font-mono">${formatCompact(post.views || post.impressions || 0)}</td>
                                            <td class="px-4 py-4 text-right text-hud-green font-mono">${formatCompact(post.engagement || 0)}</td>
                                            <td class="px-4 py-4 text-right text-hud-cyan font-mono">${formatCompact(post.clicks || 0)}</td>
                                            <td class="px-4 py-4 text-right text-white/80 font-mono">${formatCompact(post.comments || 0)} / ${formatCompact(post.reactions || 0)} / ${formatCompact(post.shares || 0)}</td>
                                            <td class="px-4 py-4"><span class="badge ${analyticsTone}" title="${escapeHtml(Object.values(post.analytics_errors || {}).join(" | "))}">${analyticsLabel}</span></td>
                                            <td class="px-4 py-4 text-right">
                                                <div class="flex items-center justify-end gap-3">
                                                    <a href="#" data-page="fb-comments" class="fb-post-comments text-xs hover:text-white" style="color:#4a9eff;"><i class="fa-solid fa-comment"></i></a>
                                                    ${post.permalink_url ? `<a href="${escapeHtml(post.permalink_url)}" target="_blank" rel="noopener noreferrer" class="text-xs text-hud-muted hover:text-white"><i class="fa-solid fa-arrow-up-right-from-square"></i></a>` : ""}
                                                </div>
                                            </td>
                                        </tr>
                                    `}).join("") || `<tr><td colspan="10" class="px-5 py-10 text-center text-hud-muted text-sm">Chưa có bài viết trong DB. Hệ thống sẽ đồng bộ nền; có bao nhiêu bài sẽ hiển thị bấy nhiêu sau khi sync xong.</td></tr>`}
                                </tbody>
                            </table>
                        </div>
                        <div class="px-5 py-4 border-t border-hud-fb/15 flex items-center justify-between text-[10px] uppercase-wide">
                            <div class="text-hud-muted">Showing ${formatNumber((payload.offset || 0) + (posts.length ? 1 : 0))}-${formatNumber((payload.offset || 0) + posts.length)} of ${formatNumber(payload.total || 0)}</div>
                            <div class="flex items-center gap-2">
                                <button id="fb-posts-prev" class="btn-ghost px-3 py-1.5 font-bold ${Number(payload.offset || 0) <= 0 ? "opacity-40 cursor-not-allowed" : ""}" ${Number(payload.offset || 0) <= 0 ? "disabled" : ""}>PREV</button>
                                <button id="fb-posts-next" class="btn-ghost px-3 py-1.5 font-bold ${payload.has_more ? "" : "opacity-40 cursor-not-allowed"}" ${payload.has_more ? "" : "disabled"}>NEXT</button>
                            </div>
                        </div>
                    </div>
                </div>
            `;
            const syncPosts = async () => {
                if (state.facebookPostsSyncing) return;
                state.facebookPostsSyncing = true;
                section.querySelector("#fb-posts-sync-status")?.classList.remove("hidden");
                const button = section.querySelector("#fb-posts-refresh");
                if (button) button.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> SYNCING`;
                try {
                    await fetchJSON("/facebook/posts/sync?limit=50", { method: "POST" });
                } finally {
                    state.facebookPostsSyncing = false;
                    await renderFacebookPostsPage();
                }
            };
            section.querySelector("#fb-posts-refresh")?.addEventListener("click", syncPosts);
            section.querySelector("#fb-posts-prev")?.addEventListener("click", () => {
                state.facebookPostsOffset = Math.max(0, state.facebookPostsOffset - state.facebookPostsLimit);
                renderFacebookPostsPage();
            });
            section.querySelector("#fb-posts-next")?.addEventListener("click", () => {
                state.facebookPostsOffset += state.facebookPostsLimit;
                renderFacebookPostsPage();
            });
            if (!posts.length && !state.facebookPostsAutoSynced) {
                state.facebookPostsAutoSynced = true;
                setTimeout(syncPosts, 50);
            }
            section.querySelectorAll(".fb-post-comments").forEach((button) => {
                button.addEventListener("click", (event) => {
                    event.preventDefault();
                    if (window.switchPage) window.switchPage("fb-comments");
                });
            });
        } catch (error) {
            section.innerHTML = `<div class="max-w-7xl mx-auto text-hud-red text-sm">Failed to load Facebook posts: ${escapeHtml(error.message)}</div>`;
        }
    }

    function facebookContentJobBadge(status) {
        const key = String(status || "").toLowerCase();
        if (["completed", "published"].includes(key)) return ["green", "COMPLETED", "fa-check"];
        if (key === "scheduled") return ["amber", "SCHEDULED", "fa-clock"];
        if (["queued", "pending"].includes(key)) return ["cyan", "QUEUED", "fa-hourglass-half"];
        if (["posting", "processing", "running"].includes(key)) return ["cyan", "POSTING", "fa-spinner fa-spin"];
        if (["failed", "error"].includes(key)) return ["red", "FAILED", "fa-triangle-exclamation"];
        return ["", String(status || "DRAFT").toUpperCase(), "fa-circle"];
    }

    function facebookContentJobResults(job) {
        return Array.isArray(job?.results) ? job.results : [];
    }

    function facebookContentJobVariants(job) {
        return Array.isArray(job?.variants) ? job.variants : [];
    }

    function facebookContentJobPageNames(job) {
        const names = new Set();
        facebookContentJobVariants(job).forEach((variant) => {
            if (variant?.page_name) names.add(String(variant.page_name));
        });
        facebookContentJobResults(job).forEach((result) => {
            if (result?.page_name) names.add(String(result.page_name));
        });
        return Array.from(names);
    }

    function facebookContentJobTitle(job) {
        const variant = facebookContentJobVariants(job)[0] || {};
        return variant.headline || variant.title || truncate(variant.caption || job?.brief || "Facebook content job", 96);
    }

    function facebookContentJobCaption(job) {
        const variant = facebookContentJobVariants(job)[0] || {};
        return variant.caption || job?.brief || "";
    }

    function facebookContentResultCounts(job) {
        const results = facebookContentJobResults(job);
        return results.reduce((acc, result) => {
            const status = String(result?.status || "").toLowerCase();
            if (status === "scheduled") acc.scheduled += 1;
            else if (["published", "completed"].includes(status)) acc.published += 1;
            else if (["failed", "error"].includes(status)) acc.failed += 1;
            else acc.pending += 1;
            return acc;
        }, { published: 0, scheduled: 0, failed: 0, pending: 0 });
    }

    function facebookContentJobScheduledLabel(job) {
        const scheduledAt = job?.scheduled_at || "";
        const mode = String(job?.schedule_mode || "").toLowerCase();
        if (scheduledAt) return `Hẹn giờ · ${formatDate(scheduledAt)}`;
        if (mode === "best_time") return "Best time AI";
        if (job?.completed_at) return `Xong · ${formatDate(job.completed_at)}`;
        if (job?.updated_at) return `Cập nhật · ${formatDate(job.updated_at)}`;
        return formatDate(job?.created_at || "");
    }

    function facebookContentResultRows(job) {
        const results = facebookContentJobResults(job);
        if (!results.length) {
            return `<div class="mt-3 text-[10px] text-hud-muted uppercase-wide">Chưa có kết quả publish. Job có thể đang nằm trong hàng đợi.</div>`;
        }
        return `
            <div class="mt-3 grid gap-2">
                ${results.map((result) => {
                    const [tone, label, icon] = facebookContentJobBadge(result.status || "");
                    const message = result.error ? redactSensitiveText(result.error) : (result.facebook_post_id || result.permalink || "");
                    return `
                        <div class="flex items-center gap-3 border border-hud-fb/10 bg-black/20 px-3 py-2 text-[10px]">
                            <span class="badge ${tone} shrink-0"><i class="fa-solid ${icon} text-[9px]"></i>${label}</span>
                            <span class="text-white font-bold truncate min-w-[120px]">${escapeHtml(result.page_name || result.page_id || "Facebook Page")}</span>
                            <span class="text-hud-muted truncate flex-1">${escapeHtml(message || "Đang chờ kết quả")}</span>
                            ${result.permalink ? `<a class="text-hud-fb hover:text-white" href="${escapeHtml(result.permalink)}" target="_blank" rel="noopener noreferrer"><i class="fa-solid fa-arrow-up-right-from-square"></i></a>` : ""}
                        </div>
                    `;
                }).join("")}
            </div>
        `;
    }

    function renderFacebookContentJobCard(job) {
        const [tone, label, icon] = facebookContentJobBadge(job.status || "");
        const counts = facebookContentResultCounts(job);
        const pageNames = facebookContentJobPageNames(job);
        const imageCount = Array.isArray(job.images) ? job.images.length : 0;
        const variantCount = facebookContentJobVariants(job).length;
        return `
            <article class="hud-card p-4 fade-in" style="border-color: rgba(74, 158, 255, 0.3);">
                <span class="c-tl" style="border-color:#4a9eff;"></span><span class="c-br" style="border-color:#4a9eff;"></span>
                <div class="flex items-start gap-4">
                    <div class="w-10 h-10 border border-hud-fb/30 bg-hud-fb/10 flex items-center justify-center text-hud-fb shrink-0">
                        <i class="fa-brands fa-facebook-f"></i>
                    </div>
                    <div class="min-w-0 flex-1">
                        <div class="flex items-start gap-3">
                            <div class="min-w-0 flex-1">
                                <div class="text-white font-black text-sm truncate">${escapeHtml(facebookContentJobTitle(job))}</div>
                                <div class="text-[10px] text-hud-muted mt-1 truncate">${escapeHtml(facebookContentJobCaption(job) || "Không có brief hiển thị")}</div>
                            </div>
                            <span class="badge ${tone} shrink-0"><i class="fa-solid ${icon} text-[9px]"></i>${label}</span>
                        </div>
                        <div class="mt-3 flex flex-wrap items-center gap-2 text-[10px] uppercase-wide">
                            <span class="badge" style="color:#4a9eff; border-color: rgba(74, 158, 255, 0.45);">JOB ${escapeHtml(String(job.job_id || "").slice(0, 8))}</span>
                            <span class="text-hud-muted"><i class="fa-solid fa-clock"></i> ${escapeHtml(facebookContentJobScheduledLabel(job))}</span>
                            <span class="text-hud-muted"><i class="fa-solid fa-layer-group"></i> ${formatNumber(variantCount)} variants</span>
                            <span class="text-hud-muted"><i class="fa-solid fa-image"></i> ${formatNumber(imageCount)} images</span>
                        </div>
                        <div class="mt-3 flex flex-wrap gap-2">
                            ${pageNames.slice(0, 6).map((name) => `<span class="badge" style="color:#4a9eff; border-color: rgba(74, 158, 255, 0.35); background: rgba(74, 158, 255, 0.08);">${escapeHtml(name)}</span>`).join("")}
                            ${pageNames.length > 6 ? `<span class="text-[10px] text-hud-muted">+${formatNumber(pageNames.length - 6)} page</span>` : ""}
                            ${!pageNames.length ? `<span class="text-[10px] text-hud-muted">Chưa có page target</span>` : ""}
                        </div>
                        <div class="mt-3 grid grid-cols-4 gap-2 text-center">
                            <div class="border border-hud-green/20 bg-hud-green/5 px-2 py-2">
                                <div class="metric-num text-lg text-hud-green">${formatNumber(counts.published)}</div>
                                <div class="text-[8px] uppercase-widest text-hud-muted">PUBLISHED</div>
                            </div>
                            <div class="border border-hud-amber/20 bg-hud-amber/5 px-2 py-2">
                                <div class="metric-num text-lg text-hud-amber">${formatNumber(counts.scheduled)}</div>
                                <div class="text-[8px] uppercase-widest text-hud-muted">SCHEDULED</div>
                            </div>
                            <div class="border border-hud-cyan/20 bg-hud-cyan/5 px-2 py-2">
                                <div class="metric-num text-lg text-hud-cyan">${formatNumber(counts.pending)}</div>
                                <div class="text-[8px] uppercase-widest text-hud-muted">PENDING</div>
                            </div>
                            <div class="border border-hud-red/20 bg-hud-red/5 px-2 py-2">
                                <div class="metric-num text-lg text-hud-red">${formatNumber(counts.failed)}</div>
                                <div class="text-[8px] uppercase-widest text-hud-muted">FAILED</div>
                            </div>
                        </div>
                        ${facebookContentResultRows(job)}
                    </div>
                </div>
            </article>
        `;
    }

    async function renderFacebookContentJobsPage() {
        const section = document.getElementById("page-fb-jobs");
        if (!section) return;
        if (state.facebookContentJobsRefreshTimer) {
            clearTimeout(state.facebookContentJobsRefreshTimer);
            state.facebookContentJobsRefreshTimer = null;
        }
        if (!section.dataset.hydrated) {
            section.innerHTML = `<div class="max-w-7xl mx-auto text-hud-muted text-sm">Đang tải danh sách job Facebook...</div>`;
        }
        try {
            const payload = await fetchJSON("/facebook/content/jobs?limit=100");
            const jobs = payload.jobs || [];
            const today = new Date().toLocaleDateString("vi-VN");
            const activeCount = jobs.filter((job) => ["queued", "pending", "posting", "processing", "running"].includes(String(job.status || "").toLowerCase())).length;
            const scheduledCount = jobs.filter((job) => String(job.status || "").toLowerCase() === "scheduled" || facebookContentResultCounts(job).scheduled > 0).length;
            const failedCount = jobs.filter((job) => String(job.status || "").toLowerCase() === "failed" || facebookContentResultCounts(job).failed > 0).length;
            const postedToday = jobs.reduce((sum, job) => {
                return sum + facebookContentJobResults(job).filter((result) => {
                    if (!["published", "completed"].includes(String(result.status || "").toLowerCase())) return false;
                    if (!result.published_at) return false;
                    return new Date(result.published_at).toLocaleDateString("vi-VN") === today;
                }).length;
            }, 0);
            section.dataset.hydrated = "1";
            section.innerHTML = `
                <div class="max-w-7xl mx-auto">
                    <div class="grid grid-cols-4 gap-4 mb-6">
                        ${[
                            ["ACTIVE JOBS", activeCount, "cyan", "fa-spinner"],
                            ["POSTED TODAY", postedToday, "green", "fa-check"],
                            ["SCHEDULED", scheduledCount, "amber", "fa-clock"],
                            ["FAILED", failedCount, "red", "fa-triangle-exclamation"],
                        ].map(([labelText, value, tone, iconName]) => `
                            <div class="hud-card ${tone === "green" ? "green" : tone === "amber" ? "amber" : tone === "red" ? "danger" : ""} p-4 fade-in" style="${tone === "cyan" ? "border-color: rgba(74, 158, 255, 0.3);" : ""}">
                                <span class="c-tl" style="${tone === "cyan" ? "border-color:#4a9eff;" : ""}"></span><span class="c-br" style="${tone === "cyan" ? "border-color:#4a9eff;" : ""}"></span>
                                <div class="text-[9px] uppercase-widest mb-1 ${tone === "green" ? "text-hud-green" : tone === "amber" ? "text-hud-amber" : tone === "red" ? "text-hud-red" : ""}" ${tone === "cyan" ? `style="color:#4a9eff;"` : ""}><i class="fa-solid ${iconName}"></i> ${labelText}</div>
                                <div class="metric-num text-3xl ${tone === "green" ? "text-hud-green" : tone === "amber" ? "text-hud-amber" : tone === "red" ? "text-hud-red" : "text-white"}">${formatNumber(value)}</div>
                            </div>
                        `).join("")}
                    </div>
                    <div class="hud-card p-3 mb-4 fade-in flex items-center gap-3" style="border-color: rgba(74, 158, 255, 0.3);">
                        <span class="c-tl" style="border-color:#4a9eff;"></span><span class="c-br" style="border-color:#4a9eff;"></span>
                        <div class="flex-1">
                            <div class="font-display font-black text-xs text-white uppercase-widest"><i class="fa-brands fa-facebook text-hud-fb"></i> FACEBOOK CONTENT JOBS</div>
                            <div class="text-[10px] text-hud-muted mt-1">${formatNumber(payload.total ?? jobs.length)} job trong database · tự refresh khi job còn chạy</div>
                        </div>
                        <button id="fb-content-jobs-refresh" class="btn-ghost px-3 py-1.5 text-[10px] uppercase-wide font-bold"><i class="fa-solid fa-rotate"></i> REFRESH</button>
                    </div>
                    <div class="space-y-4">
                        ${jobs.map(renderFacebookContentJobCard).join("") || `
                            <div class="hud-card p-10 text-center text-hud-muted text-sm" style="border-color: rgba(74, 158, 255, 0.3);">
                                <span class="c-tl" style="border-color:#4a9eff;"></span><span class="c-br" style="border-color:#4a9eff;"></span>
                                Chưa có job Facebook. Tạo job ở màn “Khởi tạo job Facebook”, danh sách này sẽ hiển thị kết quả publish/schedule theo từng fanpage.
                            </div>
                        `}
                    </div>
                </div>
            `;
            section.querySelector("#fb-content-jobs-refresh")?.addEventListener("click", () => renderFacebookContentJobsPage());
            if (activeCount > 0 && section.classList.contains("active")) {
                state.facebookContentJobsRefreshTimer = setTimeout(renderFacebookContentJobsPage, 5000);
            }
        } catch (error) {
            section.innerHTML = `<div class="max-w-7xl mx-auto text-hud-red text-sm">Failed to load Facebook jobs: ${escapeHtml(error.message)}</div>`;
        }
    }

    function facebookCommentBadge(sentiment) {
        const key = String(sentiment || "neutral").toLowerCase();
        if (key === "negative") return ["red", "NEGATIVE", "fa-user"];
        if (key === "question") return ["amber", "QUESTION", "fa-question"];
        if (key === "positive") return ["green", "POSITIVE", "fa-heart"];
        return ["cyan", "NEUTRAL", "fa-user"];
    }

    async function renderFacebookCommentsPage() {
        const section = document.getElementById("page-fb-comments");
        if (!section) return;
        if (!section.dataset.hydrated) {
            section.innerHTML = `<div class="max-w-7xl mx-auto text-hud-muted text-sm">Đang đọc cache bình luận...</div>`;
        }
        try {
            const payload = await fetchJSON("/facebook/comments?limit=50");
            const comments = payload.comments || [];
            const totals = payload.totals || {};
            if (!state.selectedFacebookCommentId && comments[0]) state.selectedFacebookCommentId = comments[0].comment_id;
            const selected = comments.find((item) => item.comment_id === state.selectedFacebookCommentId) || comments[0] || null;
            section.dataset.hydrated = "1";
            section.innerHTML = `
                <div class="max-w-7xl mx-auto">
                    <div id="fb-comments-sync-status" class="${state.facebookCommentsSyncing ? "" : "hidden"} mb-4 border border-hud-cyan/30 bg-hud-cyan/10 text-hud-cyan text-[11px] p-3">
                        Đang đồng bộ bình luận Facebook trong nền. Bình luận đã lưu sẽ vẫn hiển thị, sync xong sẽ tự cập nhật.
                    </div>
                    <div class="hud-card danger p-3 mb-4 fade-in">
                        <span class="c-tl"></span><span class="c-br"></span>
                        <div class="flex items-center gap-3">
                            <i class="fa-solid fa-bell text-hud-red ${Number(totals.negative || 0) ? "blink" : ""} text-lg"></i>
                            <div class="flex-1">
                                <div class="text-[11px] text-white font-bold uppercase-wide"><span class="text-hud-red">${formatNumber(totals.pending || 0)} BÌNH LUẬN</span> cần xem lại · ${formatNumber(totals.negative || 0)} negative · ${formatNumber(totals.question || 0)} câu hỏi</div>
                            </div>
                            <span class="badge red">MODERATION</span>
                            <button id="fb-comments-refresh" class="btn-ghost px-3 py-1.5 text-[10px] uppercase-wide font-bold"><i class="fa-solid fa-rotate"></i> REFRESH</button>
                        </div>
                    </div>
                    ${facebookWarningBanner(payload.warnings)}
                    <div class="grid gap-4" style="grid-template-columns: 360px 1fr; min-height: calc(100vh - 280px);">
                        <div class="hud-card flex flex-col overflow-hidden fade-in" style="border-color: rgba(74, 158, 255, 0.3);">
                            <span class="c-tl" style="border-color:#4a9eff;"></span><span class="c-br" style="border-color:#4a9eff;"></span>
                            <div class="header-strip px-4 py-3 flex items-center gap-2" style="background: linear-gradient(90deg, rgba(74, 158, 255, 0.15) 0%, rgba(74, 158, 255, 0.02) 50%, rgba(74, 158, 255, 0.15) 100%); border-bottom-color: rgba(74, 158, 255, 0.4);">
                                <i class="fa-solid fa-comments text-hud-fb"></i>
                                <span class="font-display font-black text-[10px] text-white uppercase-widest">BÌNH LUẬN · ${formatNumber(comments.length)}</span>
                            </div>
                            <div class="flex border-b border-hud-fb/15 text-[9px] uppercase-widest font-bold">
                                <button class="flex-1 py-2 text-white" style="background: rgba(74, 158, 255, 0.15); color:#4a9eff; border-bottom: 2px solid #4a9eff;">TẤT CẢ</button>
                                <button class="flex-1 py-2 text-hud-muted">PENDING <span class="text-hud-amber">${formatNumber(totals.pending || 0)}</span></button>
                                <button class="flex-1 py-2 text-hud-muted">NEGATIVE <span class="text-hud-red">${formatNumber(totals.negative || 0)}</span></button>
                            </div>
                            <div class="flex-1 overflow-y-auto">
                                ${comments.map((comment) => {
                                    const [tone, label, icon] = facebookCommentBadge(comment.sentiment);
                                    const active = selected && selected.comment_id === comment.comment_id;
                                    return `
                                        <button class="fb-comment-item block w-full text-left p-3 border-b border-hud-fb/10 hover:bg-hud-fb/5 ${active ? "bg-hud-fb/10" : ""}" data-comment-id="${escapeHtml(comment.comment_id)}">
                                            <div class="flex items-start gap-2.5">
                                                <div class="w-9 h-9 rounded-full bg-hud-${tone}/20 border border-hud-${tone} flex items-center justify-center flex-shrink-0">
                                                    <i class="fa-solid ${icon} text-hud-${tone} text-[10px]"></i>
                                                </div>
                                                <div class="flex-1 min-w-0">
                                                    <div class="flex items-center gap-1 mb-0.5">
                                                        <span class="text-xs font-bold text-white truncate">${escapeHtml(comment.author_name || "Facebook User")}</span>
                                                        <span class="text-[9px] text-hud-muted ml-auto">${escapeHtml(formatDate(comment.created_time))}</span>
                                                    </div>
                                                    <div class="text-[10px] text-white/70 truncate italic">"${escapeHtml(comment.message || "")}"</div>
                                                    <div class="flex items-center gap-1 mt-1.5">
                                                        <span class="badge ${tone}" style="font-size:8px;">${label}</span>
                                                        <span class="text-[8px] text-hud-muted uppercase-wide ml-1">${escapeHtml(comment.page_name || "Page")} · ${formatNumber(comment.reply_count || 0)} replies</span>
                                                    </div>
                                                </div>
                                            </div>
                                        </button>
                                    `;
                                }).join("") || `<div class="p-6 text-center text-hud-muted text-sm">Chưa có bình luận trong DB. Hệ thống sẽ đồng bộ nền; có bao nhiêu bình luận sẽ hiển thị bấy nhiêu sau khi sync xong.</div>`}
                            </div>
                        </div>
                        <div class="hud-card overflow-hidden fade-in" style="border-color: rgba(74, 158, 255, 0.3);">
                            <span class="c-tl" style="border-color:#4a9eff;"></span><span class="c-br" style="border-color:#4a9eff;"></span>
                            ${selected ? (() => {
                                const [tone, label] = facebookCommentBadge(selected.sentiment);
                                return `
                                    <div class="header-strip px-5 py-3 flex items-center gap-2" style="background: linear-gradient(90deg, rgba(74, 158, 255, 0.15) 0%, rgba(74, 158, 255, 0.02) 50%, rgba(74, 158, 255, 0.15) 100%); border-bottom-color: rgba(74, 158, 255, 0.4);">
                                        <i class="fa-solid fa-message text-hud-fb"></i>
                                        <span class="font-display font-black text-xs text-white uppercase-widest">COMMENT DETAIL</span>
                                        <span class="badge ${tone} ml-auto">${label}</span>
                                    </div>
                                    <div class="p-5 space-y-5">
                                        <div class="border border-hud-fb/15 bg-hud-fb/5 p-4">
                                            <div class="text-[10px] text-hud-muted uppercase-wide mb-2">POST PREVIEW · ${escapeHtml(selected.page_name || "Facebook Page")}</div>
                                            <div class="text-sm text-white">${escapeHtml(selected.post_message || "Bài viết không có nội dung text")}</div>
                                            ${selected.permalink_url ? `<a href="${escapeHtml(selected.permalink_url)}" target="_blank" rel="noopener noreferrer" class="inline-flex items-center gap-2 mt-3 text-[10px] uppercase-wide font-bold" style="color:#4a9eff;"><i class="fa-solid fa-arrow-up-right-from-square"></i> OPEN ON FACEBOOK</a>` : ""}
                                        </div>
                                        <div>
                                            <div class="flex items-center justify-between mb-2">
                                                <div class="text-white font-bold">${escapeHtml(selected.author_name || "Facebook User")}</div>
                                                <div class="text-[10px] text-hud-muted">${escapeHtml(formatDate(selected.created_time))}</div>
                                            </div>
                                            <div class="text-sm text-white/85 leading-relaxed border-l-2 pl-4" style="border-color:#4a9eff;">${escapeHtml(selected.message || "")}</div>
                                        </div>
                                        <div class="grid grid-cols-3 gap-3 text-[10px]">
                                            <div class="border border-hud-cyan/15 bg-black/20 p-3"><div class="text-hud-muted uppercase-wide">Likes</div><div class="text-white font-mono">${formatNumber(selected.like_count || 0)}</div></div>
                                            <div class="border border-hud-cyan/15 bg-black/20 p-3"><div class="text-hud-muted uppercase-wide">Replies</div><div class="text-white font-mono">${formatNumber(selected.reply_count || 0)}</div></div>
                                            <div class="border border-hud-cyan/15 bg-black/20 p-3"><div class="text-hud-muted uppercase-wide">Status</div><div class="text-white font-mono uppercase">${escapeHtml(selected.status || "pending")}</div></div>
                                        </div>
                                        <div class="border border-hud-fb/15 bg-black/20 p-4">
                                            <label class="text-[10px] font-bold uppercase-widest mb-2 block" style="color:#4a9eff;">Reply draft</label>
                                            <textarea class="hud-input w-full min-h-[120px] px-4 py-3 text-sm" placeholder="Tính năng trả lời tự động sẽ dùng khung này ở bước tiếp theo."></textarea>
                                            <div class="mt-3 flex gap-2">
                                                <button class="btn-primary px-4 py-2 text-[10px] uppercase-wide font-bold opacity-60 cursor-not-allowed" disabled><i class="fa-solid fa-paper-plane"></i> SEND REPLY</button>
                                                <button class="btn-ghost px-4 py-2 text-[10px] uppercase-wide font-bold opacity-60 cursor-not-allowed" disabled><i class="fa-solid fa-eye-slash"></i> HIDE</button>
                                            </div>
                                        </div>
                                    </div>
                                `;
                            })() : `<div class="p-10 text-center text-hud-muted">Chọn một bình luận để xem chi tiết.</div>`}
                        </div>
                    </div>
                </div>
            `;
            const syncComments = async () => {
                if (state.facebookCommentsSyncing) return;
                state.facebookCommentsSyncing = true;
                section.querySelector("#fb-comments-sync-status")?.classList.remove("hidden");
                const button = section.querySelector("#fb-comments-refresh");
                if (button) button.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> SYNCING`;
                try {
                    await fetchJSON("/facebook/comments/sync?limit=50", { method: "POST" });
                } finally {
                    state.facebookCommentsSyncing = false;
                    await renderFacebookCommentsPage();
                }
            };
            section.querySelector("#fb-comments-refresh")?.addEventListener("click", syncComments);
            if (!comments.length && !state.facebookCommentsAutoSynced) {
                state.facebookCommentsAutoSynced = true;
                setTimeout(syncComments, 50);
            }
            section.querySelectorAll(".fb-comment-item").forEach((button) => {
                button.addEventListener("click", () => {
                    state.selectedFacebookCommentId = button.dataset.commentId || "";
                    renderFacebookCommentsPage();
                });
            });
        } catch (error) {
            section.innerHTML = `<div class="max-w-7xl mx-auto text-hud-red text-sm">Failed to load Facebook comments: ${escapeHtml(error.message)}</div>`;
        }
    }

    async function renderStatsPage() {
        const section = document.getElementById("page-stats");
        if (!section) return;
        section.innerHTML = `<div class="max-w-7xl mx-auto text-hud-muted text-sm">Loading stats...</div>`;
        try {
            const [stats, jobsPayload] = await Promise.all([
                fetchJSON("/stats"),
                fetchJSON("/jobs?limit=100"),
            ]);
            const jobs = jobsPayload.jobs || [];
            const completed = jobs.filter((job) => job.status === "completed").length;
            const failed = jobs.filter((job) => job.status === "failed").length;
            const duplicate = jobs.filter((job) => job.status === "duplicate").length;
            section.innerHTML = `
                <div class="max-w-7xl mx-auto">
                    <div class="grid grid-cols-6 gap-4 mb-6">
                        ${[
                            ["TOTAL JOBS", stats.total_processed, "white"],
                            ["SUCCESS RATE", `${formatNumber((stats.success_rate || 0) * 100, 1)}%`, "green"],
                            ["AVG QA SCORE", formatNumber(stats.avg_qa_score || 0, 2), "white"],
                            ["AVG COST", formatMoney(stats.avg_cost_per_article_usd || 0), "amber"],
                            ["AVG TIME", `${formatNumber(stats.avg_processing_time_sec || 0, 2)}s`, "white"],
                            ["DUPLICATES", `${formatNumber((stats.duplicate_rate || 0) * 100, 1)}%`, "white"],
                        ].map(([label, value, tone]) => `
                            <div class="hud-card ${tone === "green" ? "green" : tone === "amber" ? "amber" : ""} p-4 fade-in">
                                <span class="c-tl"></span><span class="c-br"></span>
                                <div class="text-[9px] ${tone === "green" ? "text-hud-green" : tone === "amber" ? "text-hud-amber" : "text-hud-cyan"} uppercase-widest mb-1">${label}</div>
                                <div class="metric-num text-2xl ${tone === "green" ? "text-hud-green" : tone === "amber" ? "text-hud-amber" : "text-white"}">${value}</div>
                            </div>
                        `).join("")}
                    </div>
                    <div class="grid grid-cols-3 gap-5">
                        <div class="hud-card p-5"><span class="c-tl"></span><span class="c-br"></span><div class="text-[10px] font-bold text-hud-cyan uppercase-widest mb-3">STATUS BREAKDOWN</div><div class="space-y-2 text-[11px]"><div class="flex justify-between"><span class="text-hud-muted">Completed</span><span class="text-hud-green">${completed}</span></div><div class="flex justify-between"><span class="text-hud-muted">Failed</span><span class="text-hud-red">${failed}</span></div><div class="flex justify-between"><span class="text-hud-muted">Duplicate</span><span class="text-hud-cyan">${duplicate}</span></div><div class="flex justify-between"><span class="text-hud-muted">DLQ Size</span><span class="text-hud-amber">${stats.dlq_size}</span></div></div></div>
                        <div class="hud-card p-5"><span class="c-tl"></span><span class="c-br"></span><div class="text-[10px] font-bold text-hud-cyan uppercase-widest mb-3">QUALITY</div><div class="space-y-2 text-[11px]"><div class="flex justify-between"><span class="text-hud-muted">Avg QA</span><span class="text-white">${formatNumber(stats.avg_qa_score || 0, 2)}</span></div><div class="flex justify-between"><span class="text-hud-muted">Success Rate</span><span class="text-white">${formatNumber((stats.success_rate || 0) * 100, 1)}%</span></div><div class="flex justify-between"><span class="text-hud-muted">Duplicate Rate</span><span class="text-white">${formatNumber((stats.duplicate_rate || 0) * 100, 1)}%</span></div></div></div>
                        <div class="hud-card p-5"><span class="c-tl"></span><span class="c-br"></span><div class="text-[10px] font-bold text-hud-cyan uppercase-widest mb-3">COST</div><div class="space-y-2 text-[11px]"><div class="flex justify-between"><span class="text-hud-muted">Avg / Article</span><span class="text-hud-amber">${formatMoney(stats.avg_cost_per_article_usd || 0)}</span></div><div class="flex justify-between"><span class="text-hud-muted">Avg Time</span><span class="text-white">${formatNumber(stats.avg_processing_time_sec || 0, 2)}s</span></div></div></div>
                    </div>
                </div>
            `;
        } catch (error) {
            section.innerHTML = `<div class="max-w-7xl mx-auto text-hud-red text-sm">Failed to load stats: ${escapeHtml(error.message)}</div>`;
        }
    }

    async function renderSettingsPage(createdToken = null) {
        const section = document.getElementById("page-settings");
        if (!section) return;
        section.innerHTML = `<div class="max-w-5xl mx-auto text-hud-muted text-sm">Loading settings...</div>`;
        try {
            const payload = await fetchJSON("/settings/tokens");
            const tokens = payload.tokens || [];
            section.innerHTML = `
                <div class="max-w-5xl mx-auto">
                    <div class="hud-card mb-6 fade-in">
                        <span class="c-tl"></span><span class="c-br"></span>
                        <div class="header-strip px-5 py-3 flex items-center gap-2">
                            <i class="fa-solid fa-key text-hud-cyan"></i>
                            <span class="font-display font-black text-xs text-white uppercase-widest">API TOKENS</span>
                            <span class="badge amber ml-auto">EXTENSION + FACEBOOK TEST</span>
                        </div>
                        <div class="p-6 space-y-5">
                            <div id="token-feedback" class="hidden text-[11px] border p-3"></div>
                            <div class="grid grid-cols-[1fr_auto] gap-3 items-end">
                                <div>
                                    <label class="text-[10px] font-bold text-hud-cyan uppercase-widest mb-2 block">Token name</label>
                                    <div class="input-wrap">
                                        <input id="api-token-name" type="text" class="hud-input w-full px-4 py-2.5 text-sm" placeholder="Chrome Extension · Laptop cá nhân"/>
                                    </div>
                                </div>
                                <button id="api-token-create" class="btn-primary px-5 py-2.5 text-[10px] uppercase-wide font-bold">
                                    <i class="fa-solid fa-plus"></i> CREATE TOKEN
                                </button>
                            </div>
                            <div class="text-[10px] text-hud-muted space-y-1">
                                <div>Dùng token này qua header <span class="text-white font-mono">Authorization: Bearer &lt;token&gt;</span>.</div>
                                <div>Hiện hỗ trợ extension Shopee và các endpoint test Facebook như <span class="text-white font-mono">POST /api/facebook/posts/sync</span>.</div>
                            </div>
                            <div id="api-token-reveal-slot">
                                ${createdToken ? `
                                    <div class="border border-hud-amber/30 bg-hud-amber/10 p-4">
                                        <div class="text-[10px] text-hud-amber uppercase-widest font-bold mb-2">Copy Token Now</div>
                                        <div class="flex gap-2">
                                            <div class="input-wrap flex-1">
                                                <input id="new-api-token" type="text" readonly class="hud-input w-full px-4 py-2.5 text-xs font-mono" value="${escapeHtml(createdToken)}"/>
                                            </div>
                                            <button id="copy-api-token" class="btn-ghost px-4 py-2.5 text-[10px] uppercase-wide font-bold"><i class="fa-solid fa-copy"></i> COPY</button>
                                        </div>
                                        <div class="mt-3 text-[10px] text-hud-muted">
                                            Test Facebook posts sync:
                                            <code class="block mt-1 text-hud-cyan whitespace-pre-wrap break-all">curl -sS -X POST "$BASE_URL/api/facebook/posts/sync?limit=50" -H "Authorization: Bearer ${escapeHtml(createdToken)}"</code>
                                        </div>
                                    </div>
                                ` : ""}
                            </div>
                        </div>
                    </div>
                    <div class="hud-card fade-in">
                        <span class="c-tl"></span><span class="c-br"></span>
                        <div class="header-strip px-5 py-3 flex items-center gap-2">
                            <i class="fa-solid fa-shield-halved text-hud-cyan"></i>
                            <span class="font-display font-black text-xs text-white uppercase-widest">ACTIVE TOKENS</span>
                            <span class="badge cyan ml-auto">${tokens.length} TOKENS</span>
                        </div>
                        <div class="p-5">
                            <div class="space-y-3">
                                ${tokens.length ? tokens.map((token) => `
                                    <div class="border border-hud-cyan/15 bg-black/20 p-4">
                                        <div class="flex items-start gap-3">
                                            <div class="flex-1 min-w-0">
                                                <div class="text-white font-bold text-sm">${escapeHtml(token.name || "Extension Token")}</div>
                                                <div class="text-[11px] text-hud-cyan font-mono mt-1">${escapeHtml(token.token_prefix || "")}••••••••••••</div>
                                                <div class="grid grid-cols-3 gap-3 mt-3 text-[10px] text-hud-muted">
                                                    <div>Created: <span class="text-white">${escapeHtml(token.created_at ? formatDate(token.created_at) : "-")}</span></div>
                                                    <div>Last used: <span class="text-white">${escapeHtml(token.last_used_at ? formatDate(token.last_used_at) : "-")}</span></div>
                                                    <div>Status: <span class="text-hud-green">${escapeHtml(token.status || "active")}</span></div>
                                                </div>
                                            </div>
                                            <button class="btn-danger px-3 py-2 text-[10px] uppercase-wide font-bold token-delete-btn" data-token-id="${escapeHtml(token.token_id)}">
                                                <i class="fa-solid fa-trash"></i> DELETE
                                            </button>
                                        </div>
                                    </div>
                                `).join("") : `<div class="text-hud-muted text-[11px]">Chưa có token nào.</div>`}
                            </div>
                        </div>
                    </div>
                </div>
            `;

            const feedback = section.querySelector("#token-feedback");
            const revealSlot = section.querySelector("#api-token-reveal-slot");
            if (createdToken && feedback) {
                feedback.className = "text-[11px] border p-3 text-hud-green border-hud-green/30 bg-hud-green/10";
                feedback.textContent = "Token created. Sao chép ngay bây giờ vì token chỉ hiện một lần.";
                feedback.classList.remove("hidden");
            }
            revealSlot?.querySelector("#copy-api-token")?.addEventListener("click", async () => {
                const input = revealSlot.querySelector("#new-api-token");
                const value = String(input?.value || "");
                if (!value) return;
                try {
                    const copied = await copyTextToClipboard(value, input);
                    if (feedback) {
                        feedback.className = copied
                            ? "text-[11px] border p-3 text-hud-cyan border-hud-cyan/30 bg-hud-cyan/10"
                            : "text-[11px] border p-3 text-hud-amber border-hud-amber/30 bg-hud-amber/10";
                        feedback.textContent = copied ? "Token copied to clipboard." : "Không copy tự động được. Token đã được chọn, nhấn Cmd/Ctrl+C.";
                        feedback.classList.remove("hidden");
                    }
                } catch (error) {
                    input?.focus();
                    input?.select();
                    if (feedback) {
                        feedback.className = "text-[11px] border p-3 text-hud-amber border-hud-amber/30 bg-hud-amber/10";
                        feedback.textContent = "Trình duyệt chặn clipboard. Token đã được chọn, nhấn Cmd/Ctrl+C.";
                        feedback.classList.remove("hidden");
                    }
                }
            });
            section.querySelector("#api-token-create")?.addEventListener("click", async () => {
                const name = String(section.querySelector("#api-token-name")?.value || "").trim();
                try {
                    const result = await fetchJSON("/settings/tokens", {
                        method: "POST",
                        body: JSON.stringify({ name: name || "Chrome Extension" }),
                    });
                    await renderSettingsPage(result.token);
                } catch (error) {
                    if (feedback) {
                        feedback.className = "text-[11px] border p-3 text-hud-red border-hud-red/30 bg-hud-red/10";
                        feedback.textContent = `Create token failed: ${error.message}`;
                        feedback.classList.remove("hidden");
                    }
                }
            });

            section.querySelectorAll(".token-delete-btn").forEach((button) => button.addEventListener("click", async () => {
                const tokenId = button.dataset.tokenId;
                if (!tokenId) return;
                await fetchJSON(`/settings/tokens/${encodeURIComponent(tokenId)}`, { method: "DELETE" });
                await renderSettingsPage();
            }));
        } catch (error) {
            section.innerHTML = `<div class="max-w-5xl mx-auto text-hud-red text-sm">Failed to load settings: ${escapeHtml(error.message)}</div>`;
        }
    }

    function bindJobOpenLinks(root = document) {
        root.querySelectorAll(".job-open-link").forEach((button) => {
            button.addEventListener("click", (event) => {
                event.preventDefault();
                const jobId = button.dataset.jobId;
                if (!jobId) return;
                setSelectedJob(jobId);
                if (window.switchPage) window.switchPage("detail");
            });
        });
    }

    async function onPageActivated(pageKey) {
        if (pageKey !== "jobs") closeJobsStream();
        if (pageKey !== "detail") closeDetailStream();
        if (pageKey !== "fb-messages") closeFacebookMessagesStream();
        if (pageKey !== "fb-jobs" && state.facebookContentJobsRefreshTimer) {
            clearTimeout(state.facebookContentJobsRefreshTimer);
            state.facebookContentJobsRefreshTimer = null;
        }
        if (pageKey === "submit") {
            await renderSubmitSiteOptions();
            await renderRecentSubmissions();
        }
        if (pageKey === "fb-create") await renderFacebookCreateTargets();
        if (pageKey === "fb-jobs") await renderFacebookContentJobsPage();
        if (pageKey === "jobs") await renderJobsPage();
        if (pageKey === "detail") await renderDetailPage();
        if (pageKey === "dlq") await renderDlqPage();
        if (pageKey === "knowledge") await renderKnowledgePage();
        if (pageKey === "shopee") await renderShopeePage();
        if (pageKey === "website-manage") await renderWebsiteManagePage();
        if (pageKey === "fb-pages") await renderFacebookPagesPage();
        if (pageKey === "fb-stats") await renderFacebookStatsPage();
        if (pageKey === "fb-posts") await renderFacebookPostsPage();
        if (pageKey === "fb-comments") await renderFacebookCommentsPage();
        if (pageKey === "fb-messages") await renderFacebookMessagesPage();
        if (pageKey === "stats") await renderStatsPage();
        if (pageKey === "settings") await renderSettingsPage();
    }

    function bindSubmitPage() {
        const trigger = document.getElementById("submit-site-trigger");
        const dropdown = document.getElementById("submit-site-dropdown");
        trigger?.addEventListener("click", (event) => {
            event.preventDefault();
            dropdown?.classList.toggle("hidden");
        });
        document.addEventListener("click", (event) => {
            if (!dropdown || !trigger) return;
            if (dropdown.classList.contains("hidden")) return;
            if (dropdown.contains(event.target) || trigger.contains(event.target)) return;
            dropdown.classList.add("hidden");
        });
        document.getElementById("submit-enqueue")?.addEventListener("click", () => submitJob());
    }

    function bindFacebookCreatePage() {
        document.getElementById("fb-create-images")?.addEventListener("change", (event) => {
            readFacebookCreateImages(event.target.files).catch((error) => {
                setFacebookCreateFeedback("error", `Không đọc được ảnh: ${error.message}`);
            });
        });
        document.getElementById("fb-create-preview")?.addEventListener("click", () => {
            previewFacebookCreateVariants();
        });
        document.querySelectorAll('input[name="schedule"]').forEach((input) => {
            input.addEventListener("change", async () => {
                state.facebookCreateScheduleMode = input.value || "now";
                if (input.value === "scheduled") {
                    openFacebookScheduleDialog();
                }
                if (input.value === "best_time") {
                    await applyFacebookBestTimeSchedule();
                }
                if (input.value === "now") {
                    state.facebookCreateScheduledAt = "";
                }
            });
        });
        document.getElementById("fb-create-enqueue")?.addEventListener("click", async () => {
            const payload = facebookCreatePayload();
            if (!payload.brief) {
                setFacebookCreateFeedback("error", "Cần nhập content brief cho job Facebook.");
                return;
            }
            if (!state.facebookCreatePreview?.posts?.length) {
                setFacebookCreateFeedback("error", "Cần bấm PREVIEW và duyệt nội dung trước khi enqueue đăng bài.");
                return;
            }
            if (payload.schedule_mode === "scheduled" && !payload.scheduled_at) {
                openFacebookScheduleDialog();
                setFacebookCreateFeedback("warn", "Cần chọn ngày giờ trước khi hẹn giờ đăng bài.");
                return;
            }
            if (payload.schedule_mode === "best_time" && !payload.scheduled_at) {
                await applyFacebookBestTimeSchedule();
                payload.scheduled_at = state.facebookCreateScheduledAt ? new Date(state.facebookCreateScheduledAt).toISOString() : "";
                payload.scheduled_at_local = state.facebookCreateScheduledAt || "";
            }
            setSelectedFacebookCreatePages(payload.page_ids);
            setSelectedFacebookCreateGroups(payload.groups);
            const button = document.getElementById("fb-create-enqueue");
            if (button) {
                button.disabled = true;
                button.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> ENQUEUEING`;
            }
            try {
                const result = await fetchJSON("/facebook/content/jobs", {
                    method: "POST",
                    body: JSON.stringify({
                        brief: payload.brief,
                        variants: state.facebookCreatePreview.posts,
                        images: payload.images,
                        publish_status: payload.schedule_mode === "now" ? "publish" : "scheduled",
                        scheduled_at: payload.scheduled_at,
                        schedule_mode: payload.schedule_mode === "best_time" ? "best_time" : (payload.schedule_mode === "scheduled" ? "manual" : ""),
                    }),
                });
                const scheduledText = payload.schedule_mode === "now" ? "đang đăng" : `đã hẹn giờ ${payload.scheduled_at_local.replace("T", " ")}`;
                setFacebookCreateFeedback("success", `Đã enqueue job Facebook ${result.job_id}. Hệ thống ${scheduledText} ${state.facebookCreatePreview.posts.length} post.`);
            } catch (error) {
                setFacebookCreateFeedback("error", `Enqueue failed: ${error.message}`);
            } finally {
                if (button) {
                    button.disabled = false;
                    button.innerHTML = `<i class="fa-solid fa-paper-plane"></i> ENQUEUE FB JOB <i class="fa-solid fa-arrow-right"></i>`;
                }
            }
        });
    }

    document.addEventListener("DOMContentLoaded", () => {
        bindSubmitPage();
        bindFacebookCreatePage();
        const originalSwitchPage = window.switchPage;
        if (typeof originalSwitchPage === "function") {
            window.switchPage = function (pageKey) {
                originalSwitchPage(pageKey);
                onPageActivated(pageKey);
            };
        }
        const activePage = document.querySelector(".page.active")?.id?.replace("page-", "") || "submit";
        onPageActivated(activePage);
    });
})();
