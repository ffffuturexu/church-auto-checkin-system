(() => {
  const STORAGE_KEY = "church-checkin-language";
  const DEFAULT_LANGUAGE = "zh-CN";
  const translations = {
    "教会自动签到系统": "Church Automatic Check-in System", "自动签到系统入口": "Automatic Check-in System", "接待同工工作台": "Reception Workspace", "事工管理后台": "Ministry Admin", "系统调试工作台": "System Debug Workspace", "系统调试工作台（技术同工专用）": "System Debug Workspace (technical staff only)", "返回首页": "Home", "用于检查摄像头与识别线程状态，控制视频流和调试画面，并实时调整识别参数与观察签到事件。": "Monitor camera and recognition threads, control video streams and debug frames, tune recognition parameters in real time, and observe check-in events.",
    "运行状态总览": "Runtime overview", "重点看摄像头是否持续出帧、识别端是否持续产出调试帧，以及系统资源是否异常。": "Check whether the camera keeps producing frames, recognition keeps producing debug frames, and system resources remain healthy.", "流媒体控制": "Streaming controls", "启动流": "Start stream", "停止流": "Stop stream", "刷新状态": "Refresh status", "重启运行时": "Restart runtime", "RTSP 或摄像头索引(如 0)": "RTSP URL or camera index (e.g. 0)", "状态读取中...": "Loading status...", "Channel B（调试视频）": "Channel B (debug video)", "关闭调试视频": "Disable debug video", "开启调试视频": "Enable debug video", "调试视频已开启": "Debug video enabled", "调试视频已关闭": "Debug video disabled", "暂停画面": "Pause frame", "恢复画面": "Resume frame", "画面正在实时更新": "Frame is updating live", "画面已暂停在当前帧": "Frame paused", "关闭识别标注": "Disable recognition overlay", "开启识别标注": "Enable recognition overlay", "识别标注已开启": "Recognition overlay enabled", "识别标注已关闭": "Recognition overlay disabled", "超参数热更新": "Hot-update hyperparameters", "收起": "Collapse", "展开": "Expand", "当前参数": "Current parameters", "修改参数": "Edit parameters", "保存并更新参数": "Save and update parameters", "还原默认参数": "Restore default parameters", "说明": "Description", "点击“修改参数”可编辑当前参数；保存后会将修改下发到运行时。": "Select \"Edit parameters\" to edit the current values; saved changes are applied to the runtime.", "Channel A（签到/事件）": "Channel A (check-in/events)", "摄像头状态": "Camera status", "识别状态": "Recognition status", "队列负载": "Queue load", "调试视频通道": "Debug video channel", "系统资源": "System resources", "GPU 状态": "GPU status", "正常": "Healthy", "告警": "Warning", "运行中": "Running", "未运行": "Stopped", "刚刚": "just now", "帧": "Frame", "事件": "Event", "如果这里为 0，前端不会收到新画面。": "If this is 0, the frontend will not receive new frames.", "确定要重启运行时吗？这会短暂中断识别线程。": "Restart the runtime? Recognition will be interrupted briefly.", "未修改任何参数": "No parameters changed", "Channel A 已连接": "Channel A connected", "Channel A 断开，3秒后重连": "Channel A disconnected; reconnecting in 3 seconds", "调试视频开关失败: ": "Failed to toggle debug video: ", "识别标注开关失败: ": "Failed to toggle recognition overlay: ", "状态获取失败: ": "Failed to load status: ",
    "绝对阈值": "Absolute threshold", "投票阈值": "Vote threshold", "差值": "Margin", "帧跳": "Frame skip", "去重秒数": "Dedupe seconds", "投票最少样本": "Minimum vote samples", "投票窗口秒数": "Vote window seconds", "高质量票占比": "High-quality vote ratio", "未知阈值兼容参数": "Unknown threshold compatibility", "最小人脸尺寸": "Minimum face size", "待复核最小相似度": "Minimum pending similarity", "待复核最少帧数": "Minimum pending frames", "陌生人最大相似度": "Maximum stranger similarity", "陌生人窗口秒数": "Stranger window seconds", "陌生人最少帧数": "Minimum stranger frames", "最长 finalize 等待秒数": "Maximum finalize wait", "轨迹最大丢失秒数": "Maximum track age", "签到资格窗口": "Check-in eligibility window", "轨迹 IoU 门槛": "Track IoU threshold", "轨迹中心距离门槛": "Track center distance threshold", "流状态: ": "Stream: ", "摄像头线程: ": "Camera thread: ", "识别线程: ": "Recognition thread: ", "帧队列: ": "Frame queue: ", "事件队列: ": "Event queue: ", "源: ": "Source: ", "最后一帧: ": "Last frame: ", "捕获: ": "Captured: ", "重连: ": "Reconnects: ", "最后调试帧: ": "Last debug frame: ", "待处理请求: ": "Pending requests: ", "最后错误: ": "Last error: ", "显存占用: ": "VRAM usage: ", "温度: ": "Temperature: ", "功耗: ": "Power: ", "名称: ": "Name: ", "个连接": " connections", "秒前": " seconds ago",
    "主页：/": "Home: /", "管理台：/admin": "Admin: /admin", "调试台：/debug": "Debug: /debug", "接待台：/reception": "Reception: /reception", "健康检查": "Health check", "系统状态": "System status", "API 文档": "API docs", "需要本机口令验证": "Local password required",
    "会友与照片管理 + 场次签到与统计（Tab 分页）": "Member and photo management, attendance events, and statistics", "会友管理、照片库管理、场次与历史签到查询、统计看板。": "Manage members and photos, attendance events and history, and statistics.", "查看 Channel A / B、控制流媒体、热更新识别超参数。仅技术同工可进入。": "Inspect Channel A / B, control streaming, and hot-update recognition parameters. Technical staff only.", "会友 / 照片管理": "Members / Photos", "场次 / 签到 / 报表 / 统计": "Events / Attendance / Reports / Statistics", "关怀中心（施工中）": "Care center", "关怀中心": "Care center", "新增会友": "Add member",
    "姓名（仅拼音/外文名）": "Name (Latin letters only)", "中文名(可空)": "Chinese name (optional)", "性别（可空）": "Gender (optional)", "男": "Male", "女": "Female", "分组": "Group", "备注(可空)": "Notes (optional)", "新增": "Add", "刷新列表": "Refresh list", "同步全部照片到 CompreFace": "Sync all photos to CompreFace", "停用为逻辑停用，不会删除历史签到记录。": "Deactivation is logical; attendance history is retained.",
    "会友列表（点击行打开详情）": "Member list (select a row for details)", "搜索 姓名/中文名/拼音全拼/首字母/分组/备注": "Search name, Chinese name, pinyin, initials, group, or notes", "仅 active": "Active only", "全部": "All", "仅 inactive": "Inactive only", "性别: 全部": "Gender: all", "性别: 男": "Gender: male", "性别: 女": "Gender: female", "性别: 未知/未填": "Gender: unknown / blank", "照片: 全部": "Photos: all", "照片: 仅有照片": "Photos: with photo", "照片: 仅无照片": "Photos: without photo", "分组: 全部": "Groups: all", "分组: 未分组": "Groups: unassigned", "清除所有筛选条件": "Clear all filters", "点击表头可排序，再次点击切换升降序": "Select a header to sort; select again to reverse the order", "当前筛选条件：": "Current filters: ", "共筛选出": " records found", "条记录": " records",
    "姓名": "Name", "会友姓名": "Member name", "中文名": "Chinese name", "性别": "Gender", "生日": "Birthday", "年龄": "Age", "照片": "Photo", "备注": "Notes", "状态": "Status", "操作": "Actions", "查看详情": "View details", "停用会友": "Deactivate", "场次/签到数据看板": "Attendance dashboard", "活跃会友": "Active members", "活跃场次": "Active events", "签到总数": "Total check-ins", "自动签到": "Automatic check-ins", "手动补签": "Manual check-ins", "待处理异常抓拍": "Pending captures", "今年平均签到（中文）": "Average Chinese-service attendance this year", "今年平均签到（意语）": "Average Italian-service attendance this year", "近 14 个主日签到趋势": "Check-in trend for the last 14 Sundays", "中文": "Chinese", "意语": "Italian", "刷新看板": "Refresh dashboard", "热门场次 Top": "Top events", "场次": "Event", "日期": "Date", "签到数": "Check-ins",
    "场次与历史报表": "Events and attendance history", "年份: 全部": "Year: all", "月份: 全部": "Month: all", "语言: 全部": "Language: all", "语言: 中文": "Language: Chinese", "语言: 意语": "Language: Italian", "语言: 其他": "Language: other", "最多 20 条": "Up to 20", "最多 50 条": "Up to 50", "最多 100 条": "Up to 100", "最多 300 条": "Up to 300", "刷新场次": "Refresh events", "左侧为只读场次列表，点击场次后右侧刷新该场次签到记录。": "Select a read-only event on the left to view its attendance records.", "场次列表（只读）": "Events (read-only)", "未选择场次": "No event selected", "每页 20 条": "20 per page", "每页 50 条": "50 per page", "每页 100 条": "100 per page", "刷新记录": "Refresh records", "导出当前场次 CSV": "Export current event CSV", "签到时间": "Check-in time", "方式": "Method", "上一页": "Previous", "下一页": "Next",
    "生日提醒（未来 30 天）": "Birthdays (next 30 days)", "展开": "Expand", "收起": "Collapse", "刷新生日列表": "Refresh birthdays", "当前 0 人": "0 members", "下次生日": "Next birthday", "还有几天": "Days remaining", "将满年龄": "Turning age", "筛选条件": "Filters", "按姓名过滤": "Filter by name", "最近 N 个月": "Recent N months", "最少签到次数": "Minimum check-ins", "最多签到次数": "Maximum check-ins", "不限": "No limit", "快捷规则": "Quick rule", "无": "None", "新人且积极参与": "Newcomers with active participation", "近期低出席": "Low recent attendance", "参与度下滑": "Declining participation", "活跃但未分组": "Active but unassigned", "活跃但缺少照片": "Active but missing photo", "仅统计主日": "Sundays only", "排序字段": "Sort field", "风险分": "Risk score", "最近签到": "Last check-in", "签到次数": "Check-in count", "排序方向": "Sort direction", "降序": "Descending", "升序": "Ascending", "应用筛选": "Apply filters", "重置条件": "Reset filters", "导出当前名单 CSV": "Export current list CSV", "当前筛选结果：0 人": "Current results: 0 members", "推荐关怀人群": "Recommended care groups", "关怀候选名单": "Care candidates", "最近签到记录": "Recent check-ins", "近月参与分布": "Monthly participation", "每周简报": "Weekly brief", "生成并显示（干跑）": "Generate and preview (dry run)", "生成并发送（服务器端）": "Generate and send (server-side)", "会友参与详情": "Member participation details", "请选择左侧会友查看详情": "Select a member on the left to view details", "会友详情": "Member details", "关闭": "Close", "年龄（自动计算）": "Age (calculated)", "输入或选择分组": "Enter or select a group", "修改": "Edit", "保存修改": "Save changes", "取消编辑": "Cancel editing", "停用会友": "Deactivate member", "恢复 active": "Restore active", "仅同步该会友照片": "Sync this member's photos", "照片管理": "Photo management",
    "照片查找、快速补签、待处理事件确认和实时签到动态统一放在这一页，默认优先展示近期活跃且未签到的带照片会友。": "Find members, record manual check-ins, review pending cases, and monitor live attendance from one workspace.", "当前场次：": "Current event: ", "当前补签场次：": "Manual check-in event: ", "按抓拍日期自动匹配": "Match by capture date", "状态读取中...": "Loading status...", "手动查找会友": "Find a member", "支持姓名、中文名、拼音全拼、拼音首字母和语音输入。": "Search by name, Chinese name, pinyin, initials, or voice.", "输入姓名/电话/组别搜索成员": "Search by name, phone, or group", "清空": "Clear", "语音输入": "Voice input", "按照片查找": "Find by photo", "语音输入待命": "Voice input ready", "签到：": "Attendance: ", "已签到": "Checked in", "未签到": "Not checked in", "只看新人": "Newcomers only", "准备就绪": "Ready", "实时签到动态（Channel A）": "Live attendance (Channel A)", "显示模式：": "Display mode: ", "签到模式": "Check-in mode", "完整模式": "Full mode", "刷新系统状态": "Refresh system status", "清空动态": "Clear feed", "待处理队列（Pending / Unknown）": "Pending queue (Pending / Unknown)", "刷新队列": "Refresh queue", "清空队列": "Clear queue",
    "未知": "Unknown", "人工补签": "Manual check-in", "人工": "Manual", "人脸识别": "Face recognition", "人脸识别(绝对阈值)": "Face recognition (absolute threshold)", "人脸识别(投票)": "Face recognition (voting)", "待人工复核": "Pending review", "陌生人检测": "Unknown person detected", "人脸识别已离线": "Face recognition offline", "摄像头状态：正常；识别状态：运行中": "Camera: normal; recognition: running", "系统状态正常": "System status normal", "摄像头和识别均异常": "Camera and recognition unavailable", "摄像头异常": "Camera unavailable", "状态正常但未收到直播连接": "Status normal, but no live connection", "摄像头状态：异常；识别状态：已离线": "Camera: unavailable; recognition: offline", "摄像头状态：异常；识别状态：可能已离线": "Camera: unavailable; recognition: may be offline", "摄像头状态：正常；识别状态：已离线": "Camera: normal; recognition: offline", "可以继续进行签到识别。": "Check-in recognition can continue.", "请先检查视频源。": "Check the video source first.", "请通知签到同工改用人工签到": "Ask reception staff to use manual check-in", "未找到成员": "No members found", "未分组": "Unassigned", "取消签到": "Undo check-in", "签到": "Check in", "查看实时签到、异常抓拍队列、快捷搜索与手动补签。": "View live check-ins, exception snapshots, quick search, and manual check-ins.", "暂无图片": "No image", "无图片": "No photo", "暂无数据": "No data", "暂无": "None", "无更多": "No more results", "读取中": "Loading", "加载中...": "Loading...", "加载失败": "Loading failed", "实时签到动态显示模式": "Live attendance display mode", "当前场次: ": "Current event: ", "当前场次读取失败: ": "Failed to load current event: ", "请先选择场次或等待当前场次加载完成": "Select an event or wait for the current event to load", "连续主日缺席": "Consecutive Sunday absences", "近 3 个月签到": "Check-ins in the last 3 months", "风险": "Risk", "最少距上次签到天数": "Minimum days since last check-in", "最多距上次签到天数": "Maximum days since last check-in", "语言": "Language", "选择语言": "Select language", "从这里进入接待、管理和系统调试工作台。": "Access the reception, admin, and system workspaces from here.", "集中查找会友、处理补签、确认待处理事件并查看实时签到动态。": "Find members, process manual check-ins, review pending cases, and monitor live attendance.", "说明：": "Note: ", "未选择": "Not selected", "会友图片": "Member photo", "提示：周报发送由系统定时任务负责；可在调试页面修改发送邮箱配置。": "Note: weekly briefs are sent by a scheduled task; sending email settings can be changed on the debug page.", "(可空)": "(optional)", "是": "Yes", "否": "No", "当前筛选结果：": "Current results: ", "当前 0 人": "0 members", "确定要清空实时签到动态吗？此操作不可撤销。": "Clear the live feed? This cannot be undone.", "状态读取失败": "Failed to load status", "无法获取摄像头和识别状态": "Unable to get camera and recognition status", "当前日期不是主日，已自动忽略该抓拍": "Today is not Sunday; this capture was ignored", "暂无可选成员": "No selectable members", "已无更多": "No more results", "加载失败:": "Loading failed: ", "月份: ": "Month: ", "第 ": "Page ", " 页 · 共 ": " · ", " 条": " records", "风险分公式：": "Risk score formula: ", "近 N 个月签到（N 取当前筛选条件）<= 1 加 35 分，<= 3 加 20 分；": "check-ins in the last N months (N follows the current filter): <= 1 adds 35 points, <= 3 adds 20 points; ", "距上次签到为空加 40 分，> 90 天加 30 分，> 60 天加 20 分，> 30 天加 10 分；": "no last check-in adds 40 points, > 90 days adds 30 points, > 60 days adds 20 points, > 30 days adds 10 points; ", "连续主日缺席 >= 8 加 25 分，>= 4 加 15 分，>= 2 加 8 分；": "consecutive Sunday absences >= 8 add 25 points, >= 4 add 15 points, >= 2 add 8 points; ", "最近 30 天较前 60 天的参与趋势为负时按 ": "when participation trends downward over the last 30 days versus the prior 60 days, ", "加分；active 且无图片再加 8 分。最终分数限制在 0 - 100，70 分及以上为高，40 - 69 分为中，40 分以下为低。": "adds points; active members without a photo add 8 more points. The final score is capped at 0-100: 70+ is high, 40-69 is medium, and below 40 is low.", "趋势值": "trend value", "请选择左侧会友查看详情": "Select a member on the left to view details", "实时签到动态显示模式": "Live attendance display mode"
  };
  Object.assign(translations, {
    "关系组": "Relationship group", "个人": "Individuals", "视图：": "View: ",
    "照片查找视图": "Photo search view",
    "关系组独立于事工/关怀分组。添加或移除先在此处暂存，点击保存后才生效；本人离组请使用下方按钮。": "Relationship groups are separate from ministry and care groups. Additions and removals are drafts until saved; use the button below to leave the group.",
    "搜索会友": "Search members", "输入姓名等信息": "Enter a name or other information",
    "重新加载关系组": "Reload relationship group", "保存关系组": "Save relationship group",
    "本人离组": "Leave group", "移除": "Remove", "添加": "Add", "已选择": "Selected",
    "当前已加入关系组": "Currently in a relationship group", "当前未加入关系组": "Not in a relationship group",
    "；有未保存的更改": "; unsaved changes", "正在加载关系组…": "Loading relationship group…",
    "关系组尚未加载，请重新加载。": "Relationship group not loaded; reload it.",
    "关系组已保存。": "Relationship group saved.", "当前会友已离组。": "Member has left the group.",
    "正在搜索…": "Searching…", "没有找到会友。": "No members found.",
    "已停用": "Inactive", "本人": "Current member",
    "暂无同时有已签到和未签到成员的关系组": "No groups have both checked-in and not-yet-checked-in members",
    "未签到不代表未到场": "Not checked in does not mean absent",
    "关系组加载失败:": "Failed to load relationship groups:",
    "关系组数据格式错误": "Invalid relationship-group data",
    "关系组响应格式错误": "Invalid relationship-group response",
    "搜索响应格式错误": "Invalid search response",
    "直接匹配": "Direct match", "同组成员": "Group member",
    "搜索模式：个人筛选暂停": "Search mode: individual filters paused",
    "搜索结果包含同组成员。": "Search results include group members.",
    "直接匹配结果超过 30 人，请细化搜索。": "More than 30 direct matches; refine your search.",
    "搜索失败:": "Search failed:", "场次已变化，请等待状态刷新": "The event changed; wait for attendance status to refresh",
    "确认让当前会友离开关系组？此操作不会删除会友。": "Remove this member from the relationship group? The member will not be deleted.",
    "未保存的关系组更改将丢弃。": "Unsaved relationship-group changes will be discarded.",
    "重新加载将丢弃未保存的关系组更改，继续？": "Reload and discard unsaved relationship-group changes?",
    "所选成员将从其原关系组转移到当前关系组，不会合并原组。确认转移并保存？": "Move selected members from their original groups to this group without merging groups? Confirm transfer and save?",
    "以下会友已属于其他关系组，确认后将转入本组：": "These members already belong to another group and will be transferred upon confirmation: ",
    "成员列表必须包含本人且不能重复": "The member list must include the current member without duplicates",
    "会友不存在，请刷新后重试": "Member not found; reload and try again",
    "关系组已发生变化，请刷新后重试": "The relationship group changed; reload and try again",
    "签到成功": "Check-in successful", "已取消签到": "Check-in cancelled",
    "加载关系组失败：": "Failed to load relationship group: ", "搜索失败：": "Search failed: ",
    "保存失败：": "Save failed: ", "未保存：": "Not saved: ", "离组失败：": "Failed to leave group: ",
    "保存失败或结果不确定：": "Save failed or result is uncertain: ",
    "离组失败或结果不确定：": "Leaving failed or result is uncertain: ",
    "请重新加载核实后再操作。": "Reload and verify before trying again."
  });
  const languageLabels = { "zh-CN": "中文", "en-US": "English" };
  const originals = new WeakMap();
  let translating = false;

  function getLanguage() {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    return languageLabels[saved] ? saved : DEFAULT_LANGUAGE;
  }

  function translateValue(value, language) {
    if (language === DEFAULT_LANGUAGE) return value;
    let result = translations[value] || value;
    result = result.replace(/^月份:\s*(\d+)月$/, "Month: $1");
    result = result.replace(/^第\s*(\d+)\s*\/\s*(\d+)\s*页\s*·\s*共\s*(\d+)\s*条$/, "Page $1 / $2 · $3 records");
    result = result.replace(/^近\s*(\d+)\s*个月签到$/, "Check-ins in the last $1 months");
    result = result.replace(/^近\s*(\d+)\s*个月签到次数$/, "Check-in count in the last $1 months");
    result = result.replace(/^显示 (\d+) 个关系组；未签到不代表未到场$/, "Showing $1 relationship groups; not checked in does not mean absent");
    result = result.replace(/^关系组 · 已签到 (\d+) \/ 未签到 (\d+)$/, "Relationship group · Checked in $1 / Not checked in $2");
    result = result.replace(/^找到 (\d+) 位会友（最多显示 200 位，请细化搜索）。$/, "Found $1 members (maximum 200; refine your search).");
    result = result.replace(/^找到 (\d+) 位会友。$/, "Found $1 members.");
    result = result.replace(/^保存将从该关系组移除 (\d+) 位成员；本人仍在组内。确认保存？$/, "Remove $1 members from this group while keeping the current member? Confirm save?");
    Object.keys(translations).sort((a, b) => b.length - a.length).forEach((source) => {
      if (source !== value && result.includes(source)) result = result.split(source).join(translations[source]);
    });
    return result;
  }

  function remember(target, property) {
    let values = originals.get(target);
    if (!values) { values = {}; originals.set(target, values); }
    if (!(property in values)) {
      values[property] = property === "nodeValue"
        ? target.nodeValue
        : target.getAttribute?.(property) ?? target[property];
    }
    return values[property];
  }

  function translate(language) {
    translating = true;
    document.documentElement.lang = language;
    document.title = language === DEFAULT_LANGUAGE ? remember(document, "title") : translateValue(remember(document, "title"), language);
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach((node) => {
      if (node.parentElement && node.parentElement.closest("script, style")) return;
      const source = remember(node, "nodeValue");
      const translated = translateValue(source, language);
      if (node.nodeValue !== translated) node.nodeValue = translated;
    });
    document.querySelectorAll("[placeholder], [title], [aria-label]").forEach((element) => {
      ["placeholder", "title", "aria-label"].forEach((property) => {
        if (!element.hasAttribute(property)) return;
        const source = remember(element, property);
        const translated = language === DEFAULT_LANGUAGE ? source : translateValue(source, language);
        if (element.getAttribute(property) !== translated) element.setAttribute(property, translated);
      });
    });
    const selector = document.querySelector("[data-localization-selector]");
    if (selector) {
      selector.value = language;
      selector.setAttribute("aria-label", language === DEFAULT_LANGUAGE ? "选择语言" : "Select language");
    }
    translating = false;
    window.dispatchEvent(new CustomEvent("languagechange", { detail: { language } }));
  }

  function mount() {
    const style = document.createElement("style");
    style.textContent = `.localization-control { position: fixed; top: 14px; right: 16px; z-index: 1000; display: inline-flex; align-items: center; gap: 7px; padding: 6px 9px; border: 1px solid rgba(80,100,120,.22); border-radius: 9px; background: rgba(255,255,255,.92); box-shadow: 0 5px 18px rgba(30,50,70,.12); color: #34485c; font: 600 12px/1.2 sans-serif; backdrop-filter: blur(8px); } .localization-control select { min-width: 92px; margin: 0; padding: 5px 7px; border: 1px solid rgba(80,100,120,.25); border-radius: 6px; background: transparent; color: inherit; font: inherit; } @media (max-width:560px) { .localization-control { top:8px; right:8px; } .localization-control > span { display:none; } }`;
    document.head.appendChild(style);
    const control = document.createElement("label");
    control.className = "localization-control";
    control.innerHTML = `<span>语言</span><select data-localization-selector aria-label="选择语言"><option value="zh-CN">中文</option><option value="en-US">English</option></select>`;
    document.body.appendChild(control);
    control.querySelector("select").addEventListener("change", (event) => {
      const language = languageLabels[event.target.value] ? event.target.value : DEFAULT_LANGUAGE;
      window.localStorage.setItem(STORAGE_KEY, language);
      translate(language);
    });
    let translationQueued = false;
    new MutationObserver(() => {
      if (translating || translationQueued) return;
      translationQueued = true;
      queueMicrotask(() => {
        translationQueued = false;
        translate(getLanguage());
      });
    }).observe(document.body, { childList: true, subtree: true, characterData: true });
    translate(getLanguage());
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
  else mount();
  window.Localization = Object.freeze({
    getLanguage,
    translate: (value) => translateValue(value, getLanguage()),
    setLanguage(language) {
      const nextLanguage = languageLabels[language] ? language : DEFAULT_LANGUAGE;
      window.localStorage.setItem(STORAGE_KEY, nextLanguage);
      translate(nextLanguage);
    },
    getLanguages: () => Object.keys(languageLabels)
  });
})();
