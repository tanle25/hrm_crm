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
        selectedShopeeSiteIds: JSON.parse(localStorage.getItem("content_forge_shopee_site_ids") || "[]"),
        selectedShopeeItemId: localStorage.getItem("content_forge_shopee_item_id") || "",
        jobsSocket: null,
        jobsSocketReconnectTimer: null,
        jobsPollTimer: null,
        jobsReconnectAttempts: 0,
        jobsStreamActive: false,
        jobsSignature: "",
        jobsStatusFilter: "",
        jobsSortKey: "",
        jobsSortDir: "asc",
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

    function maskSecret(value) {
        const text = String(value ?? "");
        if (!text) return "-";
        if (text.length <= 6) return "••••••";
        return `${text.slice(0, 2)}••••${text.slice(-2)}`;
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
        const response = await fetch(`${API_BASE}${path}`, {
            headers: { "Content-Type": "application/json", ...(options?.headers || {}) },
            ...options,
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
            section.innerHTML = `<div class="max-w-5xl mx-auto hud-card p-6"><span class="c-tl"></span><span class="c-br"></span><div class="text-hud-muted text-sm">Chưa có job được chọn. Mở từ màn jobs hoặc submit một job mới.</div></div>`;
            return;
        }
        section.innerHTML = `<div class="max-w-7xl mx-auto text-hud-muted text-sm">Loading job detail...</div>`;
        try {
            const detail = await fetchJSON(`/job/${encodeURIComponent(state.selectedJobId)}/detail`);
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
                    <div class="hud-card p-5 mb-6 fade-in">
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
                    <div class="hud-card p-6 mb-6 fade-in">
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
                            <span class="font-display font-black text-xs text-white uppercase-widest">EXTENSION API TOKENS</span>
                            <span class="badge amber ml-auto">SHOPEE EXTENSION</span>
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
                            <div class="text-[10px] text-hud-muted">
                                Dùng token này cho extension khi gọi <span class="text-white font-mono">POST /api/shopee/products</span> qua header <span class="text-white font-mono">Authorization: Bearer &lt;token&gt;</span>.
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
        if (pageKey === "submit") {
            await renderSubmitSiteOptions();
            await renderRecentSubmissions();
        }
        if (pageKey === "jobs") await renderJobsPage();
        if (pageKey === "detail") await renderDetailPage();
        if (pageKey === "dlq") await renderDlqPage();
        if (pageKey === "knowledge") await renderKnowledgePage();
        if (pageKey === "shopee") await renderShopeePage();
        if (pageKey === "website-manage") await renderWebsiteManagePage();
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

    document.addEventListener("DOMContentLoaded", () => {
        bindSubmitPage();
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
