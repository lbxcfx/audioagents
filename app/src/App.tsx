import { ChangeEvent, DragEvent, FormEvent, PointerEvent, Suspense, lazy, useEffect, useMemo, useState } from "react";
import CommercialPlatform from "./CommercialPlatformV2";
import PublicExperience from "./PublicExperience";
import { loadPlatformAuth, platformAuthHeaders, platformAuthSubject, savePlatformAuth, type PlatformAuthSession } from "./platformAuth";

const InboundExperience = lazy(() => import("./InboundExperience"));
const InboundConsole = lazy(() => import("./InboundConsole"));
const KnowledgeConsole = lazy(() => import("./KnowledgeConsole"));
const IntegrationConsole = lazy(() => import("./IntegrationConsole"));
const ContentConsole = lazy(() => import("./ContentConsole"));
const EvaluationConsole = lazy(() => import("./EvaluationConsole"));
const WorkspaceHome = lazy(() => import("./WorkspaceHome"));
const LegalDocument = lazy(() => import("./LegalDocument"));

type ViewKey = "platform" | "dashboard" | "campaignCreate" | "campaigns" | "scripts" | "calls" | "contacts" | "sms" | "manager" | "models" | "system" | "subpage";

type Campaign = {
  id: number;
  name: string;
  status: string;
  prompt: string;
  max_concurrency: number;
  retry_limit: number;
  call_count?: number;
  completed_count?: number;
  created_at?: string;
};

type Contact = {
  id: number;
  name: string;
  phone: string;
  tags: string;
  notes: string;
  created_at?: string;
};

type Call = {
  id: number;
  phone: string;
  status: string;
  room_name: string;
  duration_sec: number;
  summary: string;
  intent_level: string;
  failure_reason?: string;
  campaign_name?: string;
  scene_name?: string;
  dialogue_label?: string;
  live_duration_sec?: number;
  caller_name?: string;
  contact_name?: string;
  created_at?: string;
};

type TaskTemplate = {
  id: number;
  name: string;
  default_prompt: string;
  max_concurrency: number;
  retry_limit: number;
  default_scene_id?: number | null;
  scene_name?: string;
  status: string;
  notes: string;
  created_at?: string;
};

type DispatchRecord = {
  id: number;
  campaign_id?: number | null;
  campaign_name?: string;
  call_id?: number | null;
  phone: string;
  contact_name: string;
  dispatch_type: string;
  status: string;
  room_name: string;
  failure_reason: string;
  created_at?: string;
};

type PushRecord = {
  id: number;
  campaign_id?: number | null;
  campaign_name?: string;
  target: string;
  push_type: string;
  content: string;
  status: string;
  failure_reason: string;
  created_at?: string;
};

type TaskStat = {
  id: number;
  name: string;
  status: string;
  total_calls: number;
  pending_calls: number;
  active_calls: number;
  completed_calls: number;
  failed_calls: number;
  high_intent_calls: number;
  answer_rate: number;
  intent_rate: number;
  avg_duration: number;
};

type Dashboard = {
  kpis: {
    total_calls: number;
    answer_rate: number;
    avg_duration: number;
    high_intent: number;
    failed: number;
    active_calls: number;
  };
  status_distribution: Array<{ status: string; total: number }>;
  intent_distribution: Array<{ intent_level: string; total: number }>;
  recent_calls: Call[];
};

type DialogueConfig = {
  nlu_enabled: boolean;
  default_scene_id: number | null;
};

type DialogueScene = {
  id: number;
  name: string;
  industry: string;
  business_type: string;
  script_type?: "common" | "variable";
  auto_break?: string;
  audit_status?: string;
  status: string;
  active_version?: number;
  active_version_id?: number;
  knowledge_count?: number;
  updated_at?: string;
  ui?: Record<string, unknown>;
  ui_json?: string;
  flow?: {
    entry_node: string;
    unknown_route?: string;
    ui?: Record<string, unknown>;
    nodes: Array<{
      id: string;
      type: string;
      name: string;
      text?: string;
      routes?: Record<string, string>;
      intent_keywords?: Record<string, string[] | string>;
      ui?: Record<string, unknown>;
    }>;
  };
  knowledge?: Array<{
    id: number;
    title: string;
    answer: string;
    keywords: string;
    hit_count: number;
    enabled: number | boolean;
  }>;
};

type DialogueScenePayload = {
  name?: string;
  industry?: string;
  business_type?: string;
  script_type?: "common" | "variable";
  auto_break?: string;
  audit_status?: string;
  status?: "draft" | "published" | "disabled";
  flow?: Record<string, unknown>;
  ui?: Record<string, unknown>;
};

type AIModelProvider = "qwen" | "doubao";

type AIModelConfig = {
  provider: AIModelProvider;
  model: string;
  voice: string;
  emotion: string;
  style: string;
  sample_text: string;
  speed: number;
  pitch: number;
  volume: number;
  updated_at?: string;
};

type AIModelCatalog = {
  providers: Array<{
    id: AIModelProvider;
    name: string;
    models: Array<{ id: string; name: string }>;
    voices: Array<{ id: string; name: string; gender?: string; description?: string; verified?: boolean; sample_url?: string }>;
    emotions: string[];
    styles: string[];
  }>;
};

type AudioRecord = {
  id: string;
  name: string;
  text: string;
  source: "upload" | "synthesis";
  audio_url: string;
  mime_type?: string;
  size?: number;
  created_at?: string;
  model_config?: Partial<AIModelConfig>;
};

const fallbackAIModelConfig: AIModelConfig = {
  provider: "qwen",
  model: "qwen3-tts-flash",
  voice: "Cherry",
  emotion: "neutral",
  style: "normal",
  sample_text: "您好，请问现在方便沟通吗？",
  speed: 1,
  pitch: 1,
  volume: 1,
};

const fallbackAIModelCatalog: AIModelCatalog = {
  providers: [
    {
      id: "qwen",
      name: "千问",
      models: [{ id: "qwen3-tts-flash", name: "qwen3-tts-flash" }, { id: "qwen-tts-latest", name: "qwen-tts-latest" }],
      voices: [
        { id: "Cherry", name: "芊悦", gender: "女声", description: "阳光积极、亲切自然小姐姐（女性）", verified: true, sample_url: "/static/qwen-voice-samples/Cherry.wav" },
        { id: "Serena", name: "苏瑶", gender: "女声", description: "温柔小姐姐（女性）", verified: true, sample_url: "/static/qwen-voice-samples/Serena.wav" },
        { id: "Ethan", name: "晨煦", gender: "男声", description: "标准普通话，带部分北方口音。阳光、温暖、活力、朝气（男性）", verified: true, sample_url: "/static/qwen-voice-samples/Ethan.wav" },
        { id: "Chelsie", name: "千雪", gender: "女声", description: "二次元虚拟女友（女性）", verified: true, sample_url: "/static/qwen-voice-samples/Chelsie.wav" },
        { id: "Momo", name: "茉兔", gender: "女声", description: "撒娇搞怪，逗你开心（女性）", verified: true, sample_url: "/static/qwen-voice-samples/Momo.wav" },
        { id: "Vivian", name: "十三", gender: "女声", description: "拽拽的、可爱的小暴躁（女性）", verified: true, sample_url: "/static/qwen-voice-samples/Vivian.wav" },
        { id: "Moon", name: "月白", gender: "男声", description: "率性帅气的月白（男性）", verified: true, sample_url: "/static/qwen-voice-samples/Moon.wav" },
        { id: "Maia", name: "四月", gender: "女声", description: "知性与温柔的碰撞（女性）", verified: true, sample_url: "/static/qwen-voice-samples/Maia.wav" },
        { id: "Kai", name: "凯", gender: "男声", description: "耳朵的一场 SPA（男性）", verified: true, sample_url: "/static/qwen-voice-samples/Kai.wav" },
        { id: "Nofish", name: "不吃鱼", gender: "男声", description: "不会翘舌音的设计师（男性）", verified: true, sample_url: "/static/qwen-voice-samples/Nofish.wav" },
        { id: "Bella", name: "萌宝", gender: "女声", description: "喝酒不打醉拳的小萝莉（女性）", verified: true, sample_url: "/static/qwen-voice-samples/Bella.wav" },
        { id: "Jennifer", name: "詹妮弗", gender: "女声", description: "品牌级、电影质感般美语女声（女性）", verified: true, sample_url: "/static/qwen-voice-samples/Jennifer.wav" },
        { id: "Ryan", name: "甜茶", gender: "男声", description: "节奏拉满，戏感炸裂，真实与张力共舞（男性）", verified: true, sample_url: "/static/qwen-voice-samples/Ryan.wav" },
        { id: "Katerina", name: "卡捷琳娜", gender: "女声", description: "御姐音色，韵律回味十足（女性）", verified: true, sample_url: "/static/qwen-voice-samples/Katerina.wav" },
        { id: "Aiden", name: "艾登", gender: "男声", description: "精通厨艺的美语大男孩（男性）", verified: true, sample_url: "/static/qwen-voice-samples/Aiden.wav" },
        { id: "Eldric Sage", name: "沧明子", gender: "男声", description: "沉稳睿智的老者，沧桑如松却心明如镜（男性）", verified: true, sample_url: "/static/qwen-voice-samples/Eldric_Sage.wav" },
        { id: "Mia", name: "乖小妹", gender: "女声", description: "温顺如春水，乖巧如初雪（女性）", verified: true, sample_url: "/static/qwen-voice-samples/Mia.wav" },
        { id: "Mochi", name: "沙小弥", gender: "男声", description: "聪明伶俐的小大人，童真未泯却早慧如禅（男性）", verified: true, sample_url: "/static/qwen-voice-samples/Mochi.wav" },
        { id: "Bellona", name: "燕铮莺", gender: "女声", description: "声音洪亮，吐字清晰，人物鲜活，听得人热血沸腾；金戈铁马入梦来，字正腔圆间尽显千面人声的江湖（女性）", verified: true, sample_url: "/static/qwen-voice-samples/Bellona.wav" },
        { id: "Vincent", name: "田叔", gender: "男声", description: "一口独特的沙哑烟嗓，一开口便道尽了千军万马与江湖豪情（男性）", verified: true, sample_url: "/static/qwen-voice-samples/Vincent.wav" },
        { id: "Bunny", name: "萌小姬", gender: "女声", description: "“萌属性”爆棚的小萝莉（女性）", verified: true, sample_url: "/static/qwen-voice-samples/Bunny.wav" },
        { id: "Neil", name: "阿闻", gender: "男声", description: "平直的基线语调，字正腔圆的咬字发音，这就是最专业的新闻主持人（男性）", verified: true, sample_url: "/static/qwen-voice-samples/Neil.wav" },
        { id: "Elias", name: "墨讲师", gender: "女声", description: "既保持学科严谨性，又通过叙事技巧将复杂知识转化为可消化的认知模块（女性）", verified: true, sample_url: "/static/qwen-voice-samples/Elias.wav" },
        { id: "Arthur", name: "徐大爷", gender: "男声", description: "被岁月和旱烟浸泡过的质朴嗓音，不疾不徐地摇开了满村的奇闻异事（男性）", verified: true, sample_url: "/static/qwen-voice-samples/Arthur.wav" },
        { id: "Nini", name: "邻家妹妹", gender: "女声", description: "糯米糍一样又软又黏的嗓音，那一声声拉长了的“哥哥”，甜得能把人的骨头都叫酥了（女性）", verified: true, sample_url: "/static/qwen-voice-samples/Nini.wav" },
        { id: "Seren", name: "小婉", gender: "女声", description: "温和舒缓的声线，助你更快地进入睡眠，晚安，好梦（女性）", verified: true, sample_url: "/static/qwen-voice-samples/Seren.wav" },
        { id: "Pip", name: "顽屁小孩", gender: "男声", description: "调皮捣蛋却充满童真的他来了，这是你记忆中的小新吗（男性）", verified: true, sample_url: "/static/qwen-voice-samples/Pip.wav" },
        { id: "Stella", name: "少女阿月", gender: "女声", description: "平时是甜到发腻的迷糊少女音，但在喊出“代表月亮消灭你”时，瞬间充满不容置疑的爱与正义（女性）", verified: true, sample_url: "/static/qwen-voice-samples/Stella.wav" },
        { id: "Bodega", name: "博德加", gender: "男声", description: "热情的西班牙大叔（男性）", verified: true, sample_url: "/static/qwen-voice-samples/Bodega.wav" },
        { id: "Sonrisa", name: "索尼莎", gender: "女声", description: "热情开朗的拉美大姐（女性）", verified: true, sample_url: "/static/qwen-voice-samples/Sonrisa.wav" },
        { id: "Alek", name: "阿列克", gender: "男声", description: "一开口，是战斗民族的冷，也是毛呢大衣下的暖（男性）", verified: true, sample_url: "/static/qwen-voice-samples/Alek.wav" },
        { id: "Dolce", name: "多尔切", gender: "男声", description: "慵懒的意大利大叔（男性）", verified: true, sample_url: "/static/qwen-voice-samples/Dolce.wav" },
        { id: "Sohee", name: "素熙", gender: "女声", description: "温柔开朗，情绪丰富的韩国欧尼（女性）", verified: true, sample_url: "/static/qwen-voice-samples/Sohee.wav" },
        { id: "Ono Anna", name: "小野杏", gender: "女声", description: "鬼灵精怪的青梅竹马（女性）", verified: true, sample_url: "/static/qwen-voice-samples/Ono_Anna.wav" },
        { id: "Lenn", name: "莱恩", gender: "男声", description: "理性是底色，叛逆藏在细节里——穿西装也听后朋克的德国青年（男性）", verified: true, sample_url: "/static/qwen-voice-samples/Lenn.wav" },
        { id: "Emilien", name: "埃米尔安", gender: "男声", description: "浪漫的法国大哥哥（男性）", verified: true, sample_url: "/static/qwen-voice-samples/Emilien.wav" },
        { id: "Andre", name: "安德雷", gender: "男声", description: "声音磁性，自然舒服、沉稳男生（男性）", verified: true, sample_url: "/static/qwen-voice-samples/Andre.wav" },
        { id: "Radio Gol", name: "拉迪奥·戈尔", gender: "男声", description: "足球诗人", verified: true, sample_url: "/static/qwen-voice-samples/Radio_Gol.wav" },
        { id: "Jada", name: "上海-阿珍", gender: "女声", description: "风风火火的沪上阿姐（女性）", verified: true, sample_url: "/static/qwen-voice-samples/Jada.wav" },
        { id: "Dylan", name: "北京-晓东", gender: "男声", description: "北京胡同里长大的少年（男性）", verified: true, sample_url: "/static/qwen-voice-samples/Dylan.wav" },
        { id: "Li", name: "南京-老李", gender: "男声", description: "耐心的瑜伽老师（男性）", verified: true, sample_url: "/static/qwen-voice-samples/Li.wav" },
        { id: "Marcus", name: "陕西-秦川", gender: "男声", description: "面宽话短，心实声沉——老陕的味道（男性）", verified: true, sample_url: "/static/qwen-voice-samples/Marcus.wav" },
        { id: "Roy", name: "闽南-阿杰", gender: "男声", description: "诙谐直爽、市井活泼的台湾哥仔形象（男性）", verified: true, sample_url: "/static/qwen-voice-samples/Roy.wav" },
        { id: "Peter", name: "天津-李彼得", gender: "男声", description: "天津相声，专业捧哏（男性）", verified: true, sample_url: "/static/qwen-voice-samples/Peter.wav" },
        { id: "Sunny", name: "四川-晴儿", gender: "女声", description: "甜到你心里的川妹子（女性）", verified: true, sample_url: "/static/qwen-voice-samples/Sunny.wav" },
        { id: "Eric", name: "四川-程川", gender: "男声", description: "一个跳脱市井的四川成都男子（男性）", verified: true, sample_url: "/static/qwen-voice-samples/Eric.wav" },
        { id: "Rocky", name: "粤语-阿强", gender: "男声", description: "幽默风趣的阿强，在线陪聊（男性）", verified: true, sample_url: "/static/qwen-voice-samples/Rocky.wav" },
        { id: "Kiki", name: "粤语-阿清", gender: "女声", description: "甜美的港妹闺蜜（女性）", verified: true, sample_url: "/static/qwen-voice-samples/Kiki.wav" },
      ],
      emotions: ["neutral", "happy", "calm", "serious", "warm"],
      styles: ["normal", "customer-service", "sales", "callback"],
    },
    {
      id: "doubao",
      name: "豆包",
      models: [{ id: "seed-tts-2.0-expressive", name: "seed-tts-2.0-expressive" }, { id: "seed-tts-2.0-standard", name: "seed-tts-2.0-standard" }],
      voices: [
        { id: "zh_female_xiaohe_uranus_bigtts", name: "小荷", gender: "女声", description: "seed-tts-2.0 接口验证通过", verified: true },
        { id: "zh_female_vv_uranus_bigtts", name: "VV", gender: "女声", description: "seed-tts-2.0 接口验证通过", verified: true },
        { id: "zh_female_peiqi_uranus_bigtts", name: "佩奇", gender: "女声", description: "seed-tts-2.0 接口验证通过", verified: true },
        { id: "zh_male_m191_uranus_bigtts", name: "M191", gender: "男声", description: "seed-tts-2.0 接口验证通过", verified: true },
        { id: "zh_male_taocheng_uranus_bigtts", name: "陶城", gender: "男声", description: "seed-tts-2.0 接口验证通过", verified: true },
        { id: "zh_male_ruyayichen_uranus_bigtts", name: "儒雅一辰", gender: "男声", description: "seed-tts-2.0 接口验证通过", verified: true },
      ],
      emotions: ["neutral", "happy", "sad", "angry", "calm", "excited"],
      styles: ["normal", "expressive", "customer-service", "sales"],
    },
  ],
};

const fallbackDashboard: Dashboard = {
  kpis: {
    total_calls: 0,
    answer_rate: 0,
    avg_duration: 0,
    high_intent: 0,
    failed: 0,
    active_calls: 0,
  },
  status_distribution: [],
  intent_distribution: [],
  recent_calls: [],
};

const menuGroups: Array<{
  title: string;
  icon: string;
  items: Array<{ label: string; view: ViewKey }>;
}> = [
  { title: "控  制  台", icon: "kongzhitai.png", items: [{ label: "商用语音平台", view: "platform" }, { label: "综合概况", view: "dashboard" }] },
  {
    title: "招商管理",
    icon: "pz_touxiang.png",
    items: [
      { label: "添加账户", view: "manager" },
      { label: "账户管理", view: "manager" },
      { label: "充值管理", view: "manager" },
      { label: "机器人管理", view: "manager" },
      { label: "线路管理", view: "manager" },
      { label: "ASR管理", view: "manager" },
      { label: "短信通道", view: "manager" },
      { label: "资费管理", view: "manager" },
      { label: "服务费管理", view: "manager" },
    ],
  },
  {
    title: "话术管理",
    icon: "huashuguanli.png",
    items: [
      { label: "话术配置", view: "scripts" },
      { label: "下发记录", view: "subpage" },
    ],
  },
  { title: "AI模型", icon: "AIlogo.png", items: [{ label: "模型选择", view: "models" }] },
  {
    title: "任务管理",
    icon: "renwuguanli.png",
    items: [
      { label: "添加任务", view: "campaignCreate" },
      { label: "任务管理", view: "campaigns" },
      { label: "任务统计", view: "dashboard" },
      { label: "号码管理", view: "contacts" },
      { label: "下发记录", view: "subpage" },
      { label: "推送记录", view: "subpage" },
      { label: "任务模板", view: "subpage" },
    ],
  },
  {
    title: "通话管理",
    icon: "tonghuajilu.png",
    items: [
      { label: "当天通话记录", view: "calls" },
      { label: "历史通话记录", view: "calls" },
    ],
  },
  {
    title: "CRM系统",
    icon: "crmxitong.png",
    items: [
      { label: "客户管理", view: "contacts" },
      { label: "坐席管理", view: "system" },
    ],
  },
  {
    title: "短信管理",
    icon: "duanxinguanli.png",
    items: [
      { label: "短信签名", view: "sms" },
      { label: "短信模板", view: "sms" },
      { label: "发送记录", view: "sms" },
      { label: "消费记录", view: "sms" },
      { label: "签名审核", view: "sms" },
      { label: "模板审核", view: "sms" },
    ],
  },
  { title: "财务管理", icon: "caiwuguanli.png", items: [{ label: "消费明细", view: "system" }] },
  { title: "系统管理", icon: "pz_xitongguanli.png", items: [{ label: "基础设置", view: "system" }] },
];

const statusText: Record<string, string> = {
  draft: "草稿",
  running: "进行中",
  queued: "已排队",
  pending: "待拨打",
  dialing: "拨号中",
  ringing: "振铃",
  active: "通话中",
  completed: "已完成",
  failed: "失败",
  no_answer: "无人接听",
  busy: "忙线",
  published: "正常",
  disabled: "停用",
};

const routeText: Record<string, string> = {
  positive: "肯定",
  negative: "否定",
  reject: "拒绝",
  neutral: "中性",
  unknown: "未识别",
};

function LegacyApp() {
  const allowLegacyReplica = import.meta.env.DEV || import.meta.env.VITE_ENABLE_LEGACY_REPLICA === "true";
  const allowApiOverride = import.meta.env.DEV || import.meta.env.VITE_ALLOW_API_OVERRIDE === "true";
  const productionLabels = new Set(["商用语音平台"]);
  const visibleMenuGroups = useMemo(
    () => allowLegacyReplica
      ? menuGroups
      : menuGroups
        .map((group) => ({ ...group, items: group.items.filter((item) => productionLabels.has(item.label)) }))
        .filter((group) => group.items.length > 0),
    [allowLegacyReplica],
  );
  const [view, setView] = useState<ViewKey>("platform");
  const [activeMenu, setActiveMenu] = useState("商用语音平台");
  const [apiBase, setApiBase] = useState(() => {
    const saved = localStorage.getItem("opsApiBase");
    const defaultBase = import.meta.env.DEV ? "http://127.0.0.1:8091" : window.location.origin;
    return allowApiOverride && saved && saved !== "http://127.0.0.1:8090" ? saved : defaultBase;
  });
  const [apiInput, setApiInput] = useState(apiBase);
  const [apiOnline, setApiOnline] = useState(false);
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const [platformAuth, setPlatformAuth] = useState<PlatformAuthSession | null>(() => loadPlatformAuth());

  const [dashboard, setDashboard] = useState<Dashboard>(fallbackDashboard);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [contacts, setContacts] = useState<Contact[]>([]);
  const [calls, setCalls] = useState<Call[]>([]);
  const [taskStats, setTaskStats] = useState<TaskStat[]>([]);
  const [taskTemplates, setTaskTemplates] = useState<TaskTemplate[]>([]);
  const [dispatchRecords, setDispatchRecords] = useState<DispatchRecord[]>([]);
  const [pushRecords, setPushRecords] = useState<PushRecord[]>([]);
  const [scenes, setScenes] = useState<DialogueScene[]>([]);
  const [dialogueConfig, setDialogueConfig] = useState<DialogueConfig | null>(null);
  const [sceneId, setSceneId] = useState<number | null>(null);
  const [scene, setScene] = useState<DialogueScene | null>(null);
  const [aiModelCatalog, setAiModelCatalog] = useState<AIModelCatalog>(fallbackAIModelCatalog);
  const [aiModelConfig, setAiModelConfig] = useState<AIModelConfig>(fallbackAIModelConfig);
  const [trainingText, setTrainingText] = useState("可以，想了解怎么接入。");
  const [trainingResult, setTrainingResult] = useState<Record<string, unknown> | null>(null);

  const api = useMemo(() => {
    async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
      const response = await fetch(`${apiBase}${path}`, {
        ...options,
        headers: {
          "Content-Type": "application/json",
          ...platformAuthHeaders(platformAuth),
          ...(options.headers || {}),
        },
      });
      if (!response.ok) throw new Error(await response.text());
      const text = await response.text();
      return (text ? JSON.parse(text) : {}) as T;
    }
    return request;
  }, [apiBase, platformAuth]);

  function notify(message: string) {
    setToast(message);
    window.setTimeout(() => setToast(""), 2600);
  }

  async function loadData() {
    setError("");
    try {
      const [health, dashboardData, campaignData, contactData, callData, statData, templateData, dispatchData, pushData] = await Promise.all([
        api<{ status: string }>("/api/health"),
        api<Dashboard>("/api/dashboard"),
        api<Campaign[]>("/api/campaigns"),
        api<Contact[]>("/api/contacts"),
        api<Call[]>("/api/calls"),
        api<TaskStat[]>("/api/task-stats"),
        api<TaskTemplate[]>("/api/task-templates"),
        api<DispatchRecord[]>("/api/dispatch-records"),
        api<PushRecord[]>("/api/push-records"),
      ]);
      setApiOnline(health.status === "ok");
      setDashboard(dashboardData);
      setCampaigns(campaignData);
      setContacts(contactData);
      setCalls(callData);
      setTaskStats(statData);
      setTaskTemplates(templateData);
      setDispatchRecords(dispatchData);
      setPushRecords(pushData);
      try {
        const [configData, sceneData] = await Promise.all([
          api<DialogueConfig>("/api/dialogue/config"),
          api<DialogueScene[]>("/api/dialogue/scenes"),
        ]);
        setDialogueConfig(configData);
        setScenes(sceneData);
        const nextSceneId = sceneId || configData.default_scene_id || sceneData[0]?.id || null;
        setSceneId(nextSceneId);
        if (nextSceneId) setScene(await api<DialogueScene>(`/api/dialogue/scenes/${nextSceneId}`));
      } catch (dialogueError) {
        setDialogueConfig(null);
        setScenes([]);
        setScene(null);
        setSceneId(null);
        setError(`话术接口暂不可用：${dialogueError instanceof Error ? dialogueError.message : "请重启新版 agent 后端"}`);
      }
      try {
        const [catalogData, modelConfigData] = await Promise.all([
          api<AIModelCatalog>("/api/ai-model/catalog"),
          api<AIModelConfig>("/api/ai-model/config"),
        ]);
        setAiModelCatalog(catalogData);
        setAiModelConfig({ ...fallbackAIModelConfig, ...modelConfigData });
      } catch {
        setAiModelCatalog(fallbackAIModelCatalog);
      }
    } catch (loadError) {
      setApiOnline(false);
      setError(loadError instanceof Error ? loadError.message : "Agents API 未连接");
    }
  }

  useEffect(() => {
    if (view !== "platform") loadData();
  }, [apiBase, platformAuth, view]);

  useEffect(() => {
    if (apiBase !== "http://127.0.0.1:8090") return;
    localStorage.setItem("opsApiBase", "http://127.0.0.1:8091");
    setApiBase("http://127.0.0.1:8091");
    setApiInput("http://127.0.0.1:8091");
  }, [apiBase]);

  async function refreshScene(nextSceneId = sceneId) {
    if (!nextSceneId) return;
    setSceneId(nextSceneId);
    setScene(await api<DialogueScene>(`/api/dialogue/scenes/${nextSceneId}`));
  }

  function openMenu(label: string, nextView: ViewKey) {
    setActiveMenu(label);
    setView(nextView);
  }

  async function createCampaign(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.currentTarget));
    await api("/api/campaigns", {
      method: "POST",
      body: JSON.stringify({
        name: data.name,
        prompt: data.prompt,
        max_concurrency: Number(data.max_concurrency || 2),
        retry_limit: Number(data.retry_limit || 1),
      }),
    });
    event.currentTarget.reset();
    await loadData();
    notify("任务已创建");
  }

  async function createContact(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await api("/api/contacts", { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(event.currentTarget))) });
    event.currentTarget.reset();
    await loadData();
    notify("客户已添加");
  }

  async function importContacts(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.currentTarget));
    const contactsToImport = String(data.contacts || "")
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => {
        const [nameOrPhone, phoneMaybe, tagsMaybe] = line.split(/[,，\s]+/).filter(Boolean);
        const phone = phoneMaybe || nameOrPhone;
        const name = phoneMaybe ? nameOrPhone : "";
        return { name, phone, tags: tagsMaybe || "批量导入", notes: "号码管理导入" };
      })
      .filter((item) => item.phone);
    const result = await api<{ created: number; skipped: number }>("/api/contacts/import", {
      method: "POST",
      body: JSON.stringify({ contacts: contactsToImport }),
    });
    event.currentTarget.reset();
    await loadData();
    notify(`导入完成：新增 ${result.created} 个，跳过 ${result.skipped} 个`);
  }

  async function createCall(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.currentTarget));
    await api("/api/calls", {
      method: "POST",
      body: JSON.stringify({
        phone: data.phone,
        campaign_id: data.campaign_id ? Number(data.campaign_id) : null,
        contact_id: data.contact_id ? Number(data.contact_id) : null,
      }),
    });
    event.currentTarget.reset();
    await loadData();
    notify("通话任务已创建");
  }

  async function enqueueCampaign(campaignId: number) {
    const result = await api<{ created_calls: number }>(`/api/campaigns/${campaignId}/enqueue`, { method: "POST" });
    await loadData();
    notify(`联系人入队完成，新增 ${result.created_calls || 0} 条通话`);
  }

  async function retryDispatch(recordId: number) {
    await api(`/api/dispatch-records/${recordId}/retry`, { method: "POST" });
    await loadData();
    notify("已重新生成下发任务");
  }

  async function createTaskTemplate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.currentTarget));
    await api("/api/task-templates", {
      method: "POST",
      body: JSON.stringify({
        name: data.name,
        default_prompt: data.default_prompt,
        max_concurrency: Number(data.max_concurrency || 2),
        retry_limit: Number(data.retry_limit || 1),
        default_scene_id: data.default_scene_id ? Number(data.default_scene_id) : null,
        status: data.status || "enabled",
        notes: data.notes || "",
      }),
    });
    event.currentTarget.reset();
    await loadData();
    notify("任务模板已保存");
  }

  async function createCampaignFromTemplate(templateId: number) {
    await api(`/api/task-templates/${templateId}/campaign`, { method: "POST" });
    await loadData();
    openMenu("任务管理", "campaigns");
    notify("已根据模板生成新任务");
  }

  async function createPushRecord(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.currentTarget));
    await api("/api/push-records", {
      method: "POST",
      body: JSON.stringify({
        campaign_id: data.campaign_id ? Number(data.campaign_id) : null,
        target: data.target || "Webhook",
        push_type: data.push_type || "Webhook",
        content: data.content || "",
      }),
    });
    event.currentTarget.reset();
    await loadData();
    notify("推送记录已创建");
  }

  async function callAction(callId: number, event: "dial" | "answer" | "hangup" | "no_answer" | "busy") {
    if (event === "dial") {
      await api(`/api/calls/${callId}/dial`, { method: "POST" });
    } else if (event === "hangup") {
      await api(`/api/calls/${callId}/hangup`, { method: "POST" });
    } else {
      await api(`/api/calls/${callId}/simulate`, {
        method: "POST",
        body: JSON.stringify({
          event,
          duration_sec: 98,
          summary: "",
          intent_level: "unknown",
        }),
      });
    }
    await loadData();
    notify(event === "dial" ? "拨号请求已提交" : event === "hangup" ? "LiveKit 挂断请求已提交" : `通话状态已更新为${statusText[event] || event}`);
  }

  async function createScene(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const newScene = await api<DialogueScene>("/api/dialogue/scenes", {
      method: "POST",
      body: JSON.stringify(Object.fromEntries(new FormData(event.currentTarget))),
    });
    event.currentTarget.reset();
    await loadData();
    await refreshScene(newScene.id);
    notify("话术已创建");
  }

  async function saveDialogueScript(payload: DialogueScenePayload) {
    const sceneIdToUpdate = payload.ui?.source_scene_id as number | undefined;
    const path = sceneIdToUpdate ? `/api/dialogue/scenes/${sceneIdToUpdate}` : "/api/dialogue/scenes";
    const method = sceneIdToUpdate ? "PUT" : "POST";
    const saved = await api<DialogueScene>(path, {
      method,
      body: JSON.stringify(payload),
    });
    await loadData();
    await refreshScene(saved.id);
    notify(sceneIdToUpdate ? "话术已更新到后端数据库" : "话术已保存到后端数据库");
    return saved;
  }

  async function saveDialogueFlow(sceneIdToUpdate: number, payload: DialogueScenePayload) {
    const saved = await api<DialogueScene>(`/api/dialogue/scenes/${sceneIdToUpdate}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    await loadData();
    await refreshScene(sceneIdToUpdate);
    notify("话术流程已保存到后端数据库，模型可加载使用");
    return saved;
  }

  async function saveDialogueUi(sceneIdToUpdate: number, ui: Record<string, unknown>) {
    const saved = await api<DialogueScene>(`/api/dialogue/scenes/${sceneIdToUpdate}`, {
      method: "PUT",
      body: JSON.stringify({ ui }),
    });
    await loadData();
    await refreshScene(sceneIdToUpdate);
    notify("场景节点已保存到后端数据库");
    return saved;
  }

  async function addKnowledge(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!sceneId) return;
    const data = Object.fromEntries(new FormData(event.currentTarget));
    await api(`/api/dialogue/scenes/${sceneId}/knowledge`, {
      method: "POST",
      body: JSON.stringify({
        title: data.title,
        answer: data.answer,
        keywords: data.keywords,
        sort_order: Number(data.sort_order || 10),
        enabled: true,
      }),
    });
    event.currentTarget.reset();
    await refreshScene();
    await loadData();
    notify("知识已保存");
  }

  async function train(event?: FormEvent<HTMLFormElement>) {
    event?.preventDefault();
    if (!sceneId) return;
    const result = await api<Record<string, unknown>>("/api/dialogue/turn", {
      method: "POST",
      body: JSON.stringify({ session_id: "ui-replica-test", scene_id: sceneId, text: trainingText, channel: "app" }),
    });
    setTrainingResult(result);
    notify("模拟测试已返回");
  }

  async function startDialogueTest() {
    if (!sceneId) return;
    const result = await api<Record<string, unknown>>("/api/dialogue/start", {
      method: "POST",
      body: JSON.stringify({ session_id: "ui-replica-test", scene_id: sceneId }),
    });
    setTrainingResult(result);
    notify("测试会话已初始化");
  }

  async function publishScene(targetSceneId = sceneId) {
    if (!targetSceneId) return;
    const result = await api<Record<string, unknown>>(`/api/dialogue/scenes/${targetSceneId}/publish`, { method: "POST" });
    setTrainingResult(result);
    setSceneId(targetSceneId);
    await loadData();
    await refreshScene(targetSceneId);
    notify("话术已下发");
  }

  async function setDefaultScene(targetSceneId = sceneId) {
    if (!targetSceneId) return;
    const next = await api<DialogueConfig>(`/api/dialogue/scenes/${targetSceneId}/default`, { method: "POST" });
    setSceneId(targetSceneId);
    setDialogueConfig(next);
    notify("默认话术已更新");
  }

  async function runMicroSipTest() {
    if (!sceneId) return;
    const result = await api<Record<string, unknown>>(`/api/dialogue/scenes/${sceneId}/microsip-test`, {
      method: "POST",
      body: JSON.stringify({ phone: "1000@127.0.0.1:5066", visible: true }),
    });
    setTrainingResult(result);
    await loadData();
    notify("MicroSIP 测试任务已创建");
  }

  async function deleteKnowledge(itemId: number) {
    await api(`/api/dialogue/knowledge/${itemId}`, { method: "DELETE" });
    await refreshScene();
    await loadData();
    notify("知识已删除");
  }

  async function saveAIModelConfig(config: AIModelConfig) {
    const saved = await api<AIModelConfig>("/api/ai-model/config", {
      method: "PUT",
      body: JSON.stringify(config),
    });
    setAiModelConfig({ ...fallbackAIModelConfig, ...saved });
    notify("AI模型配置已保存，话术节点在线试听将使用当前选择");
    return saved;
  }

  async function auditionAIModel(config: AIModelConfig) {
    const response = await api<AudioRecord>("/api/dialogue/audio/audition", {
      method: "POST",
      body: JSON.stringify({ ...config, text: config.sample_text || fallbackAIModelConfig.sample_text }),
    });
    notify("样例音频已生成");
    return response.audio_url.startsWith("http") ? response.audio_url : `${apiBase}${response.audio_url}`;
  }

  async function toggleNlu() {
    const next = await api<DialogueConfig>("/api/dialogue/config/nlu", {
      method: "POST",
      body: JSON.stringify({ enabled: !dialogueConfig?.nlu_enabled }),
    });
    setDialogueConfig(next);
    notify(next.nlu_enabled ? "NLU 已开启" : "NLU 已关闭");
  }

  function applyApi(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const next = apiInput.trim().replace(/\/$/, "");
    localStorage.setItem("opsApiBase", next);
    setApiBase(next);
  }

  async function logoutApp() {
    if (platformAuth?.mode === "bearer") {
      try {
        await api("/api/platform/auth/revoke", {
          method: "POST",
          body: JSON.stringify({ reason: "user_logout" }),
        });
      } catch {
        // Clearing the browser session remains mandatory if the control plane is offline.
      }
    }
    savePlatformAuth(null);
    setPlatformAuth(null);
    setView("platform");
    setActiveMenu("商用语音平台");
    history.replaceState({}, "", "/");
  }

  if (!platformAuth) {
    return <PublicExperience onLogin={setPlatformAuth} />;
  }

  return (
    <main className="legacy-shell">
      <aside className="legacy-sidebar">
        <div className="legacy-logo">
          <img src="/assets/images/logo_5561_565144.png" alt="logo" />
        </div>
        <nav>
          {visibleMenuGroups.map((group) => (
            <section className={`menu-block ${group.items.some((item) => item.label === activeMenu) ? "open" : ""}`} key={group.title}>
              <button className="menu-title" type="button" onClick={() => openMenu(group.items[0].label, group.items[0].view)}>
                <img src={`/assets/images/${group.icon}`} alt="" />
                <span>{group.title}</span>
                <b>+</b>
              </button>
              <div className="submenu">
                {group.items.map((item) => (
                  <button
                    className={activeMenu === item.label ? "active" : ""}
                    key={`${group.title}-${item.label}`}
                    onClick={() => openMenu(item.label, item.view)}
                    type="button"
                  >
                    {item.label}
                  </button>
                ))}
              </div>
            </section>
          ))}
        </nav>
      </aside>

      <section className="legacy-main">
        {toast ? <div className="app-toast">{toast}</div> : null}
        <header className="legacy-topbar">
          <button className="back-button" onClick={() => openMenu("商用语音平台", "platform")} title="返回商用语音平台" type="button">↶</button>
          <div className="crumb">
            <img src="/assets/images/dangqianweizhi.png" alt="" />
            <span>当前位置： 综合概况 &gt; {parentTitle(activeMenu)} &gt; </span>
            <a>{activeMenu}</a>
          </div>
          <div className="user-tools">
            <span className="avatar"></span>
            <strong>账号：{platformAuthSubject(platformAuth)}</strong>
            <button onClick={logoutApp} type="button">⏻ 退出</button>
          </div>
        </header>

        <div className="legacy-content">
          {view !== "platform" && error ? <div className="legacy-warning">Agents API 未连接或响应异常：{error}</div> : null}
          {view === "platform" ? (
            <CommercialPlatform apiBase={apiBase} auth={platformAuth} onAuthChange={setPlatformAuth} />
          ) : null}
          {view === "dashboard" && activeMenu === "任务统计" ? (
            <TaskStatsReplica stats={taskStats} campaigns={campaigns} calls={calls} />
          ) : null}
          {view === "dashboard" && activeMenu !== "任务统计" ? (
            <DashboardReplica
              dashboard={dashboard}
              campaigns={campaigns}
              calls={calls}
              scenes={scenes}
              onNavigate={openMenu}
            />
          ) : null}
          {view === "campaignCreate" ? (
            <CampaignCreateReplica campaigns={campaigns} contacts={contacts} onCreate={createCampaign} onNavigate={openMenu} />
          ) : null}
          {view === "campaigns" ? (
            <CampaignReplica campaigns={campaigns} onEnqueue={enqueueCampaign} onNavigate={openMenu} />
          ) : null}
          {view === "scripts" ? (
            <ScriptsReplica
              apiBase={apiBase}
              aiModelConfig={aiModelConfig}
              config={dialogueConfig}
              scenes={scenes}
              scene={scene}
              sceneId={sceneId}
              trainingText={trainingText}
              trainingResult={trainingResult}
              onSceneChange={refreshScene}
              onCreateScene={createScene}
              onSaveScriptScene={saveDialogueScript}
              onSaveScriptFlow={saveDialogueFlow}
              onSaveScriptUi={saveDialogueUi}
              onAddKnowledge={addKnowledge}
              onTrainingTextChange={setTrainingText}
              onTrain={train}
              onToggleNlu={toggleNlu}
              onPublish={publishScene}
              onSetDefault={setDefaultScene}
              onStartDialogue={startDialogueTest}
              onMicroSipTest={runMicroSipTest}
              onDeleteKnowledge={deleteKnowledge}
            />
          ) : null}
          {view === "models" ? (
            <AIModelReplica
              apiBase={apiBase}
              catalog={aiModelCatalog}
              config={aiModelConfig}
              onSave={saveAIModelConfig}
              onAudition={auditionAIModel}
            />
          ) : null}
          {view === "calls" ? <CallsReplica calls={calls} campaigns={campaigns} contacts={contacts} onCreate={createCall} onAction={callAction} /> : null}
          {view === "contacts" ? <ContactsReplica contacts={contacts} onCreate={createContact} onImport={importContacts} /> : null}
          {view === "manager" ? <ManagerReplica title={activeMenu} campaigns={campaigns} contacts={contacts} calls={calls} /> : null}
          {view === "sms" || view === "system" || view === "subpage" ? (
            <UnifiedSubPageReplica
              title={activeMenu}
              campaigns={campaigns}
              contacts={contacts}
              calls={calls}
              dispatchRecords={dispatchRecords}
              pushRecords={pushRecords}
              taskTemplates={taskTemplates}
              scenes={scenes}
              onRetryDispatch={retryDispatch}
              onCreatePush={createPushRecord}
              onCreateTemplate={createTaskTemplate}
              onCreateCampaignFromTemplate={createCampaignFromTemplate}
            />
          ) : null}
        </div>

        {allowApiOverride ? <form className="api-stick" onSubmit={applyApi}>
          <span className={apiOnline ? "online" : "offline"}>{apiOnline ? "Agents API 已连接" : "Agents API 离线"}</span>
          <input value={apiInput} onChange={(event) => setApiInput(event.target.value)} />
          <button type="submit">应用</button>
        </form> : <div className="api-stick"><span className={apiOnline ? "online" : "offline"}>{apiOnline ? "同源 API 已连接" : "同源 API 离线"}</span></div>}
      </section>
    </main>
  );
}

function DashboardReplica({
  dashboard,
  campaigns,
  calls,
  scenes,
  onNavigate,
}: {
  dashboard: Dashboard;
  campaigns: Campaign[];
  calls: Call[];
  scenes: DialogueScene[];
  onNavigate: (label: string, view: ViewKey) => void;
}) {
  const data = [
    ["待拨打", calls.filter((call) => call.status === "pending").length, "等待拨打号码数"],
    ["已呼叫", dashboard.kpis.total_calls, "总呼叫记录"],
    ["已接通", calls.filter((call) => call.status === "completed" || call.status === "active").length, "已接通电话总数"],
    ["接通率", `${dashboard.kpis.answer_rate}%`, "已接通电话占比"],
    ["意向客户", dashboard.kpis.high_intent, "意向客户(A+B)总数"],
  ];
  return (
    <section className="legacy-page">
      <div className="page-title"><Icon name="wodeshuju.png" /> 综合概况</div>
      <div className="stat-row">
        {data.map((item) => (
          <article key={item[0]}>
            <p>{item[0]}</p>
            <strong>{item[1]}</strong>
            <span>{item[2]}</span>
          </article>
        ))}
      </div>
      <div className="dashboard-grid">
        <section className="white-panel">
          <h2><Icon name="kuaijiecaozuo.png" /> 快捷操作</h2>
          <div className="quick-grid">
            <button onClick={() => onNavigate("添加任务", "campaignCreate")} type="button">新建任务</button>
            <button onClick={() => onNavigate("号码管理", "contacts")} type="button">导入号码</button>
            <button onClick={() => onNavigate("话术配置", "scripts")} type="button">新增话术</button>
            <button onClick={() => onNavigate("当天通话记录", "calls")} type="button">查看通话</button>
          </div>
        </section>
        <section className="white-panel">
          <h2><Icon name="renwuzhongxin.png" /> 任务概况</h2>
          <TableLite rows={[
            ["任务数", campaigns.length],
            ["话术数", scenes.length],
            ["活跃通话", dashboard.kpis.active_calls],
            ["平均通话时长", `${dashboard.kpis.avg_duration}s`],
          ]} />
        </section>
      </div>
      <section className="white-panel">
        <h2><Icon name="tonghuajilu.png" /> 最近通话记录</h2>
        <CallTable calls={dashboard.recent_calls.length ? dashboard.recent_calls : calls.slice(0, 8)} compact />
      </section>
    </section>
  );
}

function AIModelReplica({
  apiBase,
  catalog,
  config,
  onSave,
  onAudition,
}: {
  apiBase: string;
  catalog: AIModelCatalog;
  config: AIModelConfig;
  onSave: (config: AIModelConfig) => Promise<AIModelConfig>;
  onAudition: (config: AIModelConfig) => Promise<string>;
}) {
  const [draft, setDraft] = useState<AIModelConfig>({ ...fallbackAIModelConfig, ...config });
  const [audioUrl, setAudioUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [voiceAudioUrls, setVoiceAudioUrls] = useState<Record<string, string>>({});
  const [voiceAuditionBusy, setVoiceAuditionBusy] = useState("");
  const [voiceAuditionErrors, setVoiceAuditionErrors] = useState<Record<string, string>>({});
  const providers = catalog.providers.length ? catalog.providers : fallbackAIModelCatalog.providers;
  const activeProvider = providers.find((provider) => provider.id === draft.provider) || providers[0];
  const activeModel = activeProvider.models.some((model) => model.id === draft.model)
    ? draft.model
    : activeProvider.models[0]?.id || draft.model;

  useEffect(() => {
    setDraft({ ...fallbackAIModelConfig, ...config });
  }, [config.provider, config.model, config.voice, config.emotion, config.style, config.sample_text]);

  function patchDraft(next: Partial<AIModelConfig>) {
    setDraft((current) => ({ ...current, ...next }));
  }

  function selectProvider(providerId: AIModelProvider) {
    const provider = providers.find((item) => item.id === providerId) || providers[0];
    setAudioUrl("");
    setDraft((current) => ({
      ...current,
      provider: provider.id,
      model: provider.models[0]?.id || current.model,
      voice: provider.voices[0]?.id || current.voice,
      emotion: provider.emotions[0] || "neutral",
      style: provider.styles[0] || "normal",
    }));
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    try {
      const saved = await onSave(draft);
      setDraft({ ...fallbackAIModelConfig, ...saved });
    } finally {
      setBusy(false);
    }
  }

  async function audition() {
    setBusy(true);
    try {
      const url = await onAudition(draft);
      setAudioUrl(url);
      new Audio(url).play().catch(() => undefined);
    } finally {
      setBusy(false);
    }
  }

  function voiceSampleKey(voiceId: string) {
    return `${activeProvider.id}:${activeModel}:${voiceId}`;
  }

  function absoluteSampleUrl(url = "") {
    if (!url) return "";
    if (/^https?:\/\//i.test(url) || url.startsWith("data:")) return url;
    return `${apiBase}${url.startsWith("/") ? url : `/${url}`}`;
  }

  async function auditionVoice(voice: { id: string; name: string; sample_url?: string }) {
    const key = voiceSampleKey(voice.id);
    setVoiceAuditionBusy(key);
    setVoiceAuditionErrors((current) => {
      const next = { ...current };
      delete next[key];
      return next;
    });
    try {
      const officialSampleUrl = absoluteSampleUrl(voice.sample_url);
      if (officialSampleUrl) {
        setVoiceAudioUrls((current) => ({ ...current, [key]: officialSampleUrl }));
        new Audio(officialSampleUrl).play().catch(() => undefined);
        return;
      }
      const url = await onAudition({
        ...draft,
        provider: activeProvider.id,
        model: activeModel,
        voice: voice.id,
        sample_text: draft.sample_text || fallbackAIModelConfig.sample_text,
      });
      setVoiceAudioUrls((current) => ({ ...current, [key]: url }));
      new Audio(url).play().catch(() => undefined);
    } catch (error) {
      setVoiceAuditionErrors((current) => ({
        ...current,
        [key]: error instanceof Error ? error.message : "样例音频生成失败",
      }));
    } finally {
      setVoiceAuditionBusy("");
    }
  }

  return (
    <section className="legacy-page ai-model-page">
      <div className="page-title"><Icon name="AIlogo.png" /> AI模型</div>
      <section className="white-panel model-provider-panel">
        <h2>模型公司</h2>
        <div className="model-provider-tabs">
          {providers.map((provider) => (
            <button
              className={draft.provider === provider.id ? "active" : ""}
              key={provider.id}
              onClick={() => selectProvider(provider.id)}
              type="button"
            >
              {provider.name}
            </button>
          ))}
        </div>
      </section>
      <section className="white-panel model-config-panel">
        <h2>合成配置</h2>
        <form className="model-config-form" onSubmit={submit}>
          <label>
            模型
            <select value={draft.model} onChange={(event) => patchDraft({ model: event.target.value })}>
              {activeProvider.models.map((model) => <option key={model.id} value={model.id}>{model.name}</option>)}
            </select>
          </label>
          <label>
            音色
            <select value={draft.voice} onChange={(event) => patchDraft({ voice: event.target.value })}>
              {activeProvider.voices.map((voice) => <option key={voice.id} value={voice.id}>{voice.name}{voice.gender ? ` / ${voice.gender}` : ""}</option>)}
            </select>
          </label>
          <label>
            音色编码
            <input value={draft.voice} onChange={(event) => patchDraft({ voice: event.target.value })} />
          </label>
          <label>
            情绪
            <select value={draft.emotion} onChange={(event) => patchDraft({ emotion: event.target.value })}>
              {activeProvider.emotions.map((emotion) => <option key={emotion} value={emotion}>{emotion}</option>)}
            </select>
          </label>
          <label>
            风格
            <select value={draft.style} onChange={(event) => patchDraft({ style: event.target.value })}>
              {activeProvider.styles.map((style) => <option key={style} value={style}>{style}</option>)}
            </select>
          </label>
          <label>
            语速
            <input max={2} min={0.5} step={0.1} type="number" value={draft.speed} onChange={(event) => patchDraft({ speed: Number(event.target.value || 1) })} />
          </label>
          <label className="wide">
            样例文本
            <textarea rows={4} value={draft.sample_text} onChange={(event) => patchDraft({ sample_text: event.target.value })} />
          </label>
          <div className="model-actions">
            <button disabled={busy} type="submit">保存配置</button>
            <button className="plain" disabled={busy} onClick={audition} type="button">样例试听</button>
            {audioUrl ? <audio controls src={audioUrl} /> : <span>当前试听将使用右侧保存前的选择。</span>}
          </div>
        </form>
      </section>
      <section className="white-panel model-voice-panel">
        <h2>{activeProvider.name} 音色列表</h2>
        <SimpleTable
          headers={["音色名称", "音色编码", "类型", "说明", "状态", "试听", "操作"]}
          rows={activeProvider.voices.map((voice) => {
            const key = voiceSampleKey(voice.id);
            const sampleUrl = voiceAudioUrls[key];
            return [
              voice.name,
              voice.id,
              voice.gender || "-",
              voice.description || "-",
              voice.sample_url ? "已验证 / 官方样例" : voice.verified ? "已验证" : "文档候选",
              <div className="voice-preview-cell">
                <button
                  className="plain"
                  disabled={Boolean(voiceAuditionBusy)}
                  onClick={() => auditionVoice(voice)}
                  type="button"
                >
                  {voiceAuditionBusy === key ? "处理中" : voice.sample_url ? (sampleUrl ? "再次播放" : "官方试听") : (sampleUrl ? "重新试听" : "试听")}
                </button>
                {sampleUrl ? <audio controls src={sampleUrl} /> : null}
                {voiceAuditionErrors[key] ? <span className="voice-audition-error">{voiceAuditionErrors[key]}</span> : null}
              </div>,
              <div className="voice-action-cell">
                <button type="button" onClick={() => patchDraft({ voice: voice.id })}>选择</button>
              </div>,
            ];
          })}
        />
      </section>
    </section>
  );
}

function TaskStatsReplica({ stats, campaigns, calls }: { stats: TaskStat[]; campaigns: Campaign[]; calls: Call[] }) {
  const rows = stats.length ? stats : campaigns.map((campaign) => {
    const related = calls.filter((call) => call.campaign_name === campaign.name);
    const completed = related.filter((call) => call.status === "completed").length;
    const failed = related.filter((call) => ["failed", "no_answer", "busy"].includes(call.status)).length;
    const high = related.filter((call) => call.intent_level === "high").length;
    return {
      id: campaign.id,
      name: campaign.name,
      status: campaign.status,
      total_calls: related.length,
      pending_calls: related.filter((call) => call.status === "pending").length,
      active_calls: related.filter((call) => ["dialing", "ringing", "active"].includes(call.status)).length,
      completed_calls: completed,
      failed_calls: failed,
      high_intent_calls: high,
      answer_rate: related.length ? Math.round((completed / related.length) * 100) : 0,
      intent_rate: related.length ? Math.round((high / related.length) * 100) : 0,
      avg_duration: 0,
    };
  });
  return (
    <section className="legacy-page">
      <div className="page-title"><Icon name="renwuzhongxin.png" /> 任务统计</div>
      <div className="manager-summary">
        <article><span>任务总数</span><strong>{rows.length}</strong></article>
        <article><span>号码总量</span><strong>{rows.reduce((sum, row) => sum + (row.total_calls || 0), 0)}</strong></article>
        <article><span>已完成</span><strong>{rows.reduce((sum, row) => sum + (row.completed_calls || 0), 0)}</strong></article>
        <article><span>高意向</span><strong>{rows.reduce((sum, row) => sum + (row.high_intent_calls || 0), 0)}</strong></article>
      </div>
      <SimpleTable
        headers={["序号", "任务名称", "任务状态", "号码数", "待拨打", "通话中", "已完成", "失败", "高意向", "接通率", "意向率", "平均时长"]}
        rows={rows.map((item, index) => [
          index + 1,
          item.name,
          statusText[item.status] || item.status,
          item.total_calls || 0,
          item.pending_calls || 0,
          item.active_calls || 0,
          item.completed_calls || 0,
          item.failed_calls || 0,
          item.high_intent_calls || 0,
          `${item.answer_rate || 0}%`,
          `${item.intent_rate || 0}%`,
          `${item.avg_duration || 0}s`,
        ])}
      />
    </section>
  );
}

function CampaignCreateReplica({
  campaigns,
  contacts,
  onCreate,
  onNavigate,
}: {
  campaigns: Campaign[];
  contacts: Contact[];
  onCreate: (event: FormEvent<HTMLFormElement>) => void;
  onNavigate: (label: string, view: ViewKey) => void;
}) {
  const lastCampaign = campaigns[0];
  return (
    <section className="legacy-page">
      <div className="filter-title">
        <h2><Icon name="renwuguanli.png" /> 添加任务</h2>
        <div>
          <button type="button" onClick={() => onNavigate("任务管理", "campaigns")}>返回任务管理</button>
          <button type="button" className="plain" onClick={() => onNavigate("号码管理", "contacts")}>导入号码</button>
        </div>
      </div>
      <div className="create-grid">
        <section className="white-panel create-main">
          <h2>任务基础信息</h2>
          <form className="legacy-form create-form" onSubmit={onCreate}>
            <label>
              任务名称
              <input name="name" required placeholder="例如：6月招商线索首呼" />
            </label>
            <label>
              并发机器人
              <input name="max_concurrency" type="number" min={1} max={20} defaultValue={2} />
            </label>
            <label>
              重呼次数
              <input name="retry_limit" type="number" min={0} max={10} defaultValue={1} />
            </label>
            <label className="wide">
              任务备注 / Agent 提示
              <textarea
                name="prompt"
                rows={5}
                placeholder="请输入本次任务的业务背景、跟进口径、特殊约束或坐席备注。"
              />
            </label>
            <div className="form-actions wide">
              <button type="submit">保存任务</button>
              <button type="button" className="plain" onClick={() => onNavigate("任务模板", "subpage")}>从模板配置</button>
            </div>
          </form>
        </section>
        <aside className="white-panel create-side">
          <h2>创建前检查</h2>
          <dl className="table-lite">
            <div><dt>可用客户</dt><dd>{contacts.length}</dd></div>
            <div><dt>已有任务</dt><dd>{campaigns.length}</dd></div>
            <div><dt>默认线路</dt><dd>LiveKit SIP</dd></div>
            <div><dt>默认 ASR</dt><dd>Qwen Realtime</dd></div>
          </dl>
          <div className="check-list">
            <p><b>1</b><span>先创建任务，再到号码管理导入或维护号码。</span></p>
            <p><b>2</b><span>任务保存后，可在任务管理中执行联系人入队。</span></p>
            <p><b>3</b><span>通话过程和下发状态会同步到通话记录、下发记录和任务统计。</span></p>
          </div>
          {lastCampaign ? <p className="muted">最近创建：{lastCampaign.name}</p> : null}
        </aside>
      </div>
    </section>
  );
}

function CampaignReplica({ campaigns, onEnqueue, onNavigate }: { campaigns: Campaign[]; onEnqueue: (id: number) => void; onNavigate: (label: string, view: ViewKey) => void }) {
  const active = campaigns[0];
  return (
    <section className="legacy-page">
      <div className="filter-title">
        <h2><Icon name="renwuguanli.png" /> 任务管理</h2>
        <div>
          <select><option>选择任务状态</option><option>人工暂停</option><option>待启动</option></select>
          <input placeholder="请输入任务名称" />
          <button>查询</button>
          <button type="button" onClick={() => onNavigate("添加任务", "campaignCreate")}>新建任务</button>
          <button>刷新列表</button>
          <button className="plain">暂停所有任务</button>
        </div>
      </div>
      <div className="task-layout">
        <aside className="task-list">
          <h2>我的任务</h2>
          {campaigns.map((campaign) => (
            <article className={campaign.id === active?.id ? "active" : ""} key={campaign.id}>
              <strong>{campaign.name}</strong>
              <div><span></span><b>进度{campaign.completed_count || 0}/{campaign.call_count || 0}</b></div>
              <p>任务状态：{statusText[campaign.status] || campaign.status}</p>
              <p>创建时间：{campaign.created_at || "-"}</p>
            </article>
          ))}
        </aside>
        <section className="task-detail">
          <header>
            <h2>{active?.name || "测试"} <a>（任务详情）</a></h2>
            <div>
              <button>开始任务</button>
              <button>导入号码</button>
              <button>导出全部号码</button>
              <button>编辑任务</button>
              <button onClick={() => active && onEnqueue(active.id)} type="button">联系人入队</button>
            </div>
          </header>
          <div className="detail-box">
            <p><b>任务状态：</b>{active ? statusText[active.status] || active.status : "人工暂停"}</p>
            <p><b>话术模板：</b>通用</p>
            <p><b>拨打线路：</b>LiveKit SIP / MicroSIP</p>
            <p><b>ASR：</b>Qwen Realtime</p>
            <p><b>占用机器人个数：</b>{active?.max_concurrency || 1}个</p>
            <p><b>接通率：</b>{active?.call_count ? Math.round(((active.completed_count || 0) / active.call_count) * 100) : 0}%</p>
            <p><b>重复呼叫次数：</b>{active?.retry_limit || 0}</p>
            <p><b>最后拨打时间：</b>未拨打</p>
            <p className="wide"><b>备注：</b>{active?.prompt || "您好，感谢您的接听。稍后我们的客户经理马上加您的微信，劳烦通过。"}</p>
          </div>
          <ProgressColumns />
        </section>
      </div>
    </section>
  );
}

function ScriptsReplica(props: {
  apiBase: string;
  aiModelConfig: AIModelConfig;
  config: DialogueConfig | null;
  scenes: DialogueScene[];
  scene: DialogueScene | null;
  sceneId: number | null;
  trainingText: string;
  trainingResult: Record<string, unknown> | null;
  onSceneChange: (id: number) => void;
  onCreateScene: (event: FormEvent<HTMLFormElement>) => void;
  onSaveScriptScene: (payload: DialogueScenePayload) => Promise<DialogueScene>;
  onSaveScriptFlow: (sceneId: number, payload: DialogueScenePayload) => Promise<DialogueScene>;
  onSaveScriptUi: (sceneId: number, ui: Record<string, unknown>) => Promise<DialogueScene>;
  onAddKnowledge: (event: FormEvent<HTMLFormElement>) => void;
  onTrainingTextChange: (value: string) => void;
  onTrain: (event: FormEvent<HTMLFormElement>) => void;
  onToggleNlu: () => void;
  onPublish: (sceneId?: number) => Promise<void>;
  onSetDefault: (sceneId?: number) => Promise<void>;
  onStartDialogue: () => void;
  onMicroSipTest: () => void;
  onDeleteKnowledge: (id: number) => void;
}) {
  type ScriptCard = { id: number; name: string; industry: string; audit: string; updated: string; status: "正常" | "异常"; type: "common" | "variable"; autoBreak?: "是" | "否"; persisted?: boolean };
  type ScriptNode = {
    id: number;
    name: string;
    kind: "flow" | "jump";
    sceneId: number;
    prompt: string;
    label?: string;
    nextStep?: string;
    target?: string;
    pauseMs?: number;
    branches?: string[];
    intentKeywords?: Record<string, string[]>;
    routes?: Record<string, number>;
    audioSource?: "upload" | "recording";
    audioRecordSource?: "all" | "upload" | "synthesis";
    audioRecordId?: string;
    audioUrl?: string;
    audioName?: string;
    audioText?: string;
    x: number;
    y: number;
  };
  type AudioRecord = { id: string; name: string; text: string; source: "upload" | "synthesis"; audio_url: string; mime_type?: string; size?: number; created_at?: string };
  type SceneNode = { id: number; scriptId: number; name: string; group: "normal" | "public" };
  type VariableRow = { id: number; scriptId: number; name: string; annotation: string; example: string };
  type ModalKey = null | "script" | "copy" | "import" | "backup" | "dispatch" | "sceneNode" | "flowNode" | "jumpNode" | "simulate" | "audio" | "knowledge" | "semantic" | "voiceExcel" | "voiceZip" | "grade" | "learning" | "variable" | "notice";

  const [scriptType, setScriptType] = useState<"common" | "variable">("common");
  const [activeScript, setActiveScript] = useState(7);
  const [activeTab, setActiveTab] = useState("流程");
  const [scriptCards, setScriptCards] = useState<ScriptCard[]>([
    { id: 32, name: "62351324", industry: "其他", audit: "提交审核", updated: "2026-06-23 20:33:48", status: "正常", type: "common", autoBreak: "否" },
    { id: 29, name: "通用", industry: "其他", audit: "审核通过", updated: "2025-06-21 22:44:18", status: "异常", type: "common", autoBreak: "否" },
    { id: 27, name: "张伟test", industry: "请选择行业", audit: "待审核", updated: "2025-08-04 17:09:34", status: "异常", type: "common", autoBreak: "否" },
    { id: 25, name: "下次", industry: "汽车", audit: "待审核", updated: "2024-12-12 14:22:24", status: "异常", type: "common", autoBreak: "否" },
    { id: 24, name: "d1", industry: "其他", audit: "待审核", updated: "2025-06-21 22:28:55", status: "异常", type: "common", autoBreak: "否" },
    { id: 22, name: "新品卷烟的推广力度", industry: "其他", audit: "待审核", updated: "2024-11-12 17:08:35", status: "异常", type: "common", autoBreak: "否" },
    { id: 20, name: "新品发布宣传回访", industry: "其他", audit: "待审核", updated: "2024-12-07 16:23:52", status: "异常", type: "common", autoBreak: "否" },
    { id: 18, name: "满意度回访", industry: "其他", audit: "审核通过", updated: "2025-08-14 12:57:08", status: "正常", type: "common", autoBreak: "否" },
  ]);
  const [sceneNodes, setSceneNodes] = useState<SceneNode[]>([
    { id: 121, scriptId: 18, name: "开场", group: "normal" },
    { id: 154, scriptId: 18, name: "骑手", group: "normal" },
    { id: 122, scriptId: 18, name: "结束语", group: "public" },
  ]);
  const [activeSceneId, setActiveSceneId] = useState(121);
  const [nodes, setNodes] = useState<ScriptNode[]>([
    { id: 375, sceneId: 121, kind: "flow", name: "流程节点", prompt: "你好", branches: ["否定", "拒绝", "肯定", "中性", "未识别"], pauseMs: 10000, x: 40, y: 60 },
    { id: 376, sceneId: 121, kind: "jump", name: "跳转节点", prompt: "", nextStep: "指定主动流程", target: "结束语", pauseMs: 3000, x: 170, y: 330 },
  ]);
  const [editingNodeId, setEditingNodeId] = useState<number | null>(null);
  const [editingScriptIndex, setEditingScriptIndex] = useState<number | null>(null);
  const defaultBranches = ["否定", "拒绝", "肯定", "中性", "未识别"];
  const [nodeBranchDrafts, setNodeBranchDrafts] = useState<string[]>(defaultBranches);
  const [newBranchName, setNewBranchName] = useState("");
  const [nodeIntentKeywordDrafts, setNodeIntentKeywordDrafts] = useState<Record<string, string[]>>({});
  const [editingBranchRule, setEditingBranchRule] = useState<string | null>(null);
  const [branchRuleInput, setBranchRuleInput] = useState("");
  const [nodePromptDraft, setNodePromptDraft] = useState("");
  const [nodeAudioSource, setNodeAudioSource] = useState<"upload" | "recording">("upload");
  const [nodeRecordSource, setNodeRecordSource] = useState<"all" | "upload" | "synthesis">("all");
  const [nodeSelectedAudioId, setNodeSelectedAudioId] = useState("");
  const [nodeAudioUrl, setNodeAudioUrl] = useState("");
  const [nodeAudioName, setNodeAudioName] = useState("");
  const [audioRecords, setAudioRecords] = useState<AudioRecord[]>([]);
  const [audioBusy, setAudioBusy] = useState(false);
  const [nodeAudioMessage, setNodeAudioMessage] = useState("");
  const [flowLabels, setFlowLabels] = useState(["肯定", "否定", "拒绝", "未识别"]);
  const [knowledge, setKnowledge] = useState([
    { title: "产品价格", question: "价格怎么算", priority: 10, keywords: "价格,费用,资费", label: "资费", updated: "2025-08-14 12:57:08" },
    { title: "加微信", question: "能不能加微信", priority: 8, keywords: "微信,联系,客户经理", label: "跟进", updated: "2025-08-14 12:57:08" },
  ]);
  const [knowledgeLabels, setKnowledgeLabels] = useState(["资费", "跟进", "异议"]);
  const [semanticLabels, setSemanticLabels] = useState(["有意向", "无意向", "稍后联系"]);
  const [records, setRecords] = useState([
    { name: "开场白", type: "合成音频", content: "您好，请问现在方便沟通吗？" },
    { name: "结束语", type: "上传音频", content: "感谢接听，祝您生活愉快。" },
  ]);
  const [grades, setGrades] = useState([
    { name: "A级", description: "明确意向客户" },
    { name: "B级", description: "一般意向客户" },
    { name: "C级", description: "简单对话" },
    { name: "D级", description: "无有效对话" },
    { name: "E级", description: "有效未接通" },
    { name: "F级", description: "无效号码" },
  ]);
  const [trainingRows, setTrainingRows] = useState([
    { text: "现在不方便", result: "稍后联系", status: "待训练" },
    { text: "价格怎么算", result: "命中知识库", status: "已处理" },
  ]);
  const [processLabelTab, setProcessLabelTab] = useState<"main" | "branch">("main");
  const [learningFilter, setLearningFilter] = useState("全部");
  const [modal, setModal] = useState<ModalKey>(null);
  const [notice, setNotice] = useState("操作已完成");
  const [variables, setVariables] = useState<VariableRow[]>([]);
  const [variableKeyword, setVariableKeyword] = useState("");
  const [editingVariableId, setEditingVariableId] = useState<number | null>(null);
  const [selectedVariableIds, setSelectedVariableIds] = useState<number[]>([]);
  const [connectingFrom, setConnectingFrom] = useState<{ nodeId: number; branch: string; x: number; y: number } | null>(null);
  const [fullscreen, setFullscreen] = useState(false);
  const [savedAt, setSavedAt] = useState("");
  const [endpointCenters, setEndpointCenters] = useState<Record<string, { x: number; y: number }>>({});
  const [chat, setChat] = useState([
    { role: "ai", text: "您好，这里是智能语音客服，请问现在方便沟通吗？" },
  ]);
  const [chatInput, setChatInput] = useState("");
  const [simulateNodeId, setSimulateNodeId] = useState<number | null>(null);
  const tabs = scriptType === "variable"
    ? ["流程", "流程标签", "知识库", "知识库标签", "语义标签", "录音管理", "等级分类", "人机训练", "变量管理", "系统配置"]
    : ["流程", "流程标签", "知识库", "知识库标签", "语义标签", "录音管理", "等级分类", "人机训练", "系统配置"];
  const activeCard = scriptCards[activeScript] || scriptCards[0];
  const editingCard = editingScriptIndex === null ? null : scriptCards[editingScriptIndex];
  const visibleScriptCards = scriptCards
    .map((scene, index) => ({ scene, index }))
    .filter(({ scene }) => scene.type === scriptType);
  const activeSceneNodes = sceneNodes.filter((node) => node.scriptId === activeCard?.id);
  const normalSceneNodes = activeSceneNodes.filter((node) => node.group !== "public");
  const publicSceneNodes = activeSceneNodes.filter((node) => node.group === "public");
  const canvasNodes = nodes.filter((node) => node.sceneId === activeSceneId);
  const canvasNodeMap = new Map(canvasNodes.map((node) => [node.id, node]));
  const connectionLines = canvasNodes.flatMap((node) => {
    if (node.kind !== "flow" || !node.routes) return [];
    return Object.entries(node.routes).flatMap(([branch, targetId]) => {
      const target = canvasNodeMap.get(targetId);
      if (!target) return [];
      return [{ source: node, branch, target }];
    });
  });
  const editingNode = editingNodeId === null ? null : nodes.find((node) => node.id === editingNodeId) || null;
  const currentScene = activeSceneNodes.find((node) => node.id === activeSceneId);
  const currentVariables = variables.filter((item) => item.scriptId === activeCard?.id);
  const filteredVariables = currentVariables.filter((item) => {
    const keyword = variableKeyword.trim();
    if (!keyword) return true;
    return [item.name, item.annotation, item.example].some((value) => value.includes(keyword));
  });
  const editingVariable = editingVariableId === null ? null : variables.find((item) => item.id === editingVariableId) || null;

  function parseSceneUi(sceneData: DialogueScene | null) {
    if (!sceneData) return {};
    if (sceneData.ui && typeof sceneData.ui === "object") return sceneData.ui;
    if (sceneData.ui_json) {
      try {
        return JSON.parse(sceneData.ui_json) as Record<string, unknown>;
      } catch {
        return {};
      }
    }
    return {};
  }

  function mapBackendSceneToCard(sceneData: DialogueScene): ScriptCard {
    return {
      id: sceneData.id,
      name: sceneData.name,
      industry: sceneData.industry || "其他",
      audit: sceneData.audit_status || (sceneData.status === "published" ? "审核通过" : "待审核"),
      updated: sceneData.updated_at || "",
      status: sceneData.status === "published" ? "正常" : "异常",
      type: sceneData.script_type === "variable" ? "variable" : "common",
      autoBreak: sceneData.auto_break === "是" ? "是" : "否",
      persisted: true,
    };
  }

  function routeKeyToBranch(route: string) {
    const map: Record<string, string> = {
      positive: "肯定",
      negative: "否定",
      reject: "拒绝",
      neutral: "中性",
      unknown: "未识别",
    };
    return map[route] || route;
  }

  function branchToRouteKey(branch: string) {
    const map: Record<string, string> = {
      肯定: "positive",
      否定: "negative",
      拒绝: "reject",
      中性: "neutral",
      未识别: "unknown",
    };
    return map[branch.trim()] || branch.trim();
  }

  function splitRuleKeywords(value: string) {
    return value.split(/[,，;；\n]/).map((item) => item.trim()).filter(Boolean);
  }

  function normalizeIntentKeywords(keywords: Record<string, unknown> = {}) {
    return Object.fromEntries(Object.entries(keywords).map(([key, value]) => {
      const items = Array.isArray(value)
        ? value.flatMap((item) => splitRuleKeywords(String(item || "")))
        : splitRuleKeywords(String(value || ""));
      return [key, Array.from(new Set(items))];
    }).filter(([, value]) => (value as string[]).length)) as Record<string, string[]>;
  }

  function keywordsForBranch(branch: string, source: Record<string, string[]> = nodeIntentKeywordDrafts) {
    const key = branchToRouteKey(branch);
    return source[key] || source[branch] || [];
  }

  function cleanIntentKeywordsForBranches(branches: string[], source: Record<string, string[]> = nodeIntentKeywordDrafts) {
    return Object.fromEntries(branches.map((branch) => {
      const key = branchToRouteKey(branch);
      const keywords = Array.from(new Set(keywordsForBranch(branch, source).map((item) => item.trim()).filter(Boolean)));
      return [key, keywords];
    }).filter(([, keywords]) => (keywords as string[]).length)) as Record<string, string[]>;
  }

  function stripInvisibleRoutes(sourceNodes: ScriptNode[]) {
    const idsByScene = new Map<number, Set<number>>();
    sourceNodes.forEach((node) => {
      const ids = idsByScene.get(node.sceneId) || new Set<number>();
      ids.add(node.id);
      idsByScene.set(node.sceneId, ids);
    });
    return sourceNodes.map((node) => {
      if (node.kind !== "flow" || !node.routes) return node;
      const validTargetIds = idsByScene.get(node.sceneId) || new Set<number>();
      const branchSet = new Set(node.branches || []);
      const routes = Object.fromEntries(Object.entries(node.routes).filter(([branch, targetId]) => (
        branchSet.has(branch) && validTargetIds.has(targetId)
      )));
      return Object.keys(routes).length === Object.keys(node.routes).length ? node : { ...node, routes };
    });
  }

  function missingBranchMessage(node: ScriptNode, branch: string) {
    return `流程节点“${node.name}”的“${branch}”分支未连接。`;
  }

  function visibleRouteTarget(source: ScriptNode | undefined, branch: string, map = canvasNodeMap) {
    const targetId = source?.routes?.[branch];
    return targetId ? map.get(targetId) || null : null;
  }

  function stableNodeId(sceneDataId: number, rawId: string) {
    const numberText = rawId.replace(/\D/g, "");
    if (numberText) return Number(numberText.slice(-9));
    let hash = 0;
    for (let index = 0; index < rawId.length; index += 1) {
      hash = ((hash << 5) - hash) + rawId.charCodeAt(index);
      hash |= 0;
    }
    return sceneDataId * 100000 + Math.abs(hash % 90000);
  }

  function hydrateSceneWorkspace(sceneData: DialogueScene) {
    const ui = parseSceneUi(sceneData);
    const uiSceneNodes = Array.isArray(ui.scene_nodes) ? ui.scene_nodes as SceneNode[] : [];
    const uiCanvasNodes = Array.isArray(ui.canvas_nodes) ? ui.canvas_nodes as ScriptNode[] : [];
    const uiVariables = Array.isArray(ui.variables) ? ui.variables as VariableRow[] : [];
    if (uiSceneNodes.length) {
      const nextSceneNodes = uiSceneNodes.map((item) => ({ ...item, scriptId: sceneData.id }));
      const sceneIds = new Set(nextSceneNodes.map((item) => item.id));
      const nextCanvasNodes = stripInvisibleRoutes(uiCanvasNodes.map((item) => ({ ...item, sceneId: item.sceneId || nextSceneNodes[0].id })));
      setSceneNodes((items) => [...items.filter((item) => item.scriptId !== sceneData.id), ...nextSceneNodes]);
      setNodes((items) => [
        ...items.filter((item) => !sceneIds.has(item.sceneId)),
        ...nextCanvasNodes,
      ]);
      setVariables((items) => [...items.filter((item) => item.scriptId !== sceneData.id), ...uiVariables.map((item) => ({ ...item, scriptId: sceneData.id }))]);
      setActiveSceneId(nextSceneNodes[0]?.id || 0);
      return;
    }

    const fallbackSceneNode: SceneNode = { id: sceneData.id * 1000 + 1, scriptId: sceneData.id, name: "开场", group: "normal" };
    const flowNodes = sceneData.flow?.nodes || [];
    const idMap = new Map(flowNodes.map((item) => [item.id, stableNodeId(sceneData.id, item.id)]));
    const nextCanvasNodes: ScriptNode[] = flowNodes
      .filter((item) => item.type !== "llm_fallback")
      .map((item, index) => {
        const routes = item.routes || {};
        const branches = Object.keys(routes).map(routeKeyToBranch);
        const uiNode = (item.ui || {}) as Partial<ScriptNode>;
        const nodeId = idMap.get(item.id) || stableNodeId(sceneData.id, item.id);
        return {
          id: nodeId,
          sceneId: fallbackSceneNode.id,
          kind: item.type === "end" ? "jump" : "flow",
          name: item.name || (item.type === "end" ? "跳转节点" : "流程节点"),
          prompt: item.text || "",
          audioSource: uiNode.audioSource || "upload",
          audioRecordSource: uiNode.audioRecordSource || "all",
          audioRecordId: uiNode.audioRecordId || "",
          audioUrl: uiNode.audioUrl || "",
          audioName: uiNode.audioName || "",
          audioText: uiNode.audioText || item.text || "",
          label: uiNode.label || "",
          nextStep: uiNode.nextStep || "",
          target: uiNode.target || "",
          pauseMs: uiNode.pauseMs || 10000,
          branches: item.type === "scene" ? branches : undefined,
          intentKeywords: item.type === "scene" ? normalizeIntentKeywords(item.intent_keywords || uiNode.intentKeywords || {}) : undefined,
          routes: item.type === "scene"
            ? Object.fromEntries(Object.entries(routes).map(([route, target]) => [routeKeyToBranch(route), idMap.get(target)]).filter(([, target]) => target))
            : undefined,
          x: Number(uiNode.x ?? 40 + (index % 2) * 300),
          y: Number(uiNode.y ?? 60 + Math.floor(index / 2) * 190),
        };
      });
    setSceneNodes((items) => [...items.filter((item) => item.scriptId !== sceneData.id), fallbackSceneNode]);
    setNodes((items) => [...items.filter((item) => item.sceneId !== fallbackSceneNode.id), ...nextCanvasNodes]);
    setActiveSceneId(fallbackSceneNode.id);
  }

  useEffect(() => {
    if (!props.scenes.length) return;
    const backendCards = props.scenes.map(mapBackendSceneToCard);
    const currentId = scriptCards[activeScript]?.id;
    const nextActive = Math.max(0, backendCards.findIndex((item) => item.id === currentId));
    setScriptCards(backendCards);
    setActiveScript(nextActive);
    setScriptType(backendCards[nextActive]?.type || "common");
  }, [props.scenes]);

  useEffect(() => {
    if (!activeCard?.persisted) return;
    if (props.sceneId !== activeCard.id) {
      props.onSceneChange(activeCard.id);
    }
  }, [activeCard?.id, activeCard?.persisted, props.sceneId]);

  useEffect(() => {
    if (!props.scene || props.scene.id !== activeCard?.id) return;
    hydrateSceneWorkspace(props.scene);
  }, [props.scene?.id, props.scene?.active_version_id, props.scene?.updated_at]);

  useEffect(() => {
    if (modal !== "flowNode") return;
    const branches = Array.from(new Set([...defaultBranches, ...(editingNode?.branches || [])]));
    setNodeBranchDrafts(branches);
    setNodeIntentKeywordDrafts(normalizeIntentKeywords(editingNode?.intentKeywords || {}));
    setEditingBranchRule(null);
    setBranchRuleInput("");
    setNewBranchName("");
  }, [modal, editingNode?.id]);

  useEffect(() => {
    if (modal !== "flowNode" && modal !== "jumpNode") return;
    setNodePromptDraft(editingNode?.prompt || "");
    setNodeAudioSource(editingNode?.audioSource || "upload");
    setNodeRecordSource(editingNode?.audioRecordSource || "all");
    setNodeSelectedAudioId(editingNode?.audioRecordId || "");
    setNodeAudioUrl(editingNode?.audioUrl || "");
    setNodeAudioName(editingNode?.audioName || "");
    setNodeAudioMessage("");
    if ((editingNode?.audioSource || "upload") === "recording") {
      loadNodeAudioRecords(editingNode?.audioRecordSource || "all");
    }
  }, [modal, editingNode?.id]);

  useEffect(() => {
    const currentBelongsToScript = sceneNodes.some((node) => node.id === activeSceneId && node.scriptId === activeCard?.id);
    if (!currentBelongsToScript) {
      const firstScene = sceneNodes.find((node) => node.scriptId === activeCard?.id);
      setActiveSceneId(firstScene?.id || 0);
    }
  }, [activeCard?.id, activeSceneId, sceneNodes]);

  useEffect(() => {
    if (activeCard?.type !== "variable" && activeTab === "变量管理") {
      setActiveTab("流程");
    }
    setVariableKeyword("");
    setSelectedVariableIds([]);
  }, [activeCard?.id, activeCard?.type, activeTab]);

  useEffect(() => {
    if (activeTab !== "流程") return;
    const frame = window.requestAnimationFrame(() => {
      const canvas = document.querySelector(".flow-canvas-replica") as HTMLDivElement | null;
      if (!canvas) return;
      const canvasRect = canvas.getBoundingClientRect();
      const next: Record<string, { x: number; y: number }> = {};
      canvas.querySelectorAll<HTMLElement>("[data-connect-target]").forEach((element) => {
        const rect = element.getBoundingClientRect();
        const nodeId = element.dataset.connectTarget;
        if (!nodeId) return;
        next[`node-${nodeId}`] = {
          x: rect.left - canvasRect.left + canvas.scrollLeft + rect.width / 2,
          y: rect.top - canvasRect.top + canvas.scrollTop + rect.height / 2,
        };
      });
      canvas.querySelectorAll<HTMLElement>("[data-branch-source][data-branch-name]").forEach((element) => {
        const rect = element.getBoundingClientRect();
        const nodeId = element.dataset.branchSource;
        const branch = element.dataset.branchName;
        if (!nodeId || !branch) return;
        next[`branch-${nodeId}-${branch}`] = {
          x: rect.left - canvasRect.left + canvas.scrollLeft + rect.width / 2,
          y: rect.top - canvasRect.top + canvas.scrollTop + rect.height / 2,
        };
      });
      setEndpointCenters((current) => JSON.stringify(current) === JSON.stringify(next) ? current : next);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [activeTab, activeSceneId, activeCard?.id, canvasNodes, fullscreen]);

  function openNotice(message: string) {
    setNotice(message);
    setModal("notice");
  }

  function audioApiUrl(path: string) {
    return `${props.apiBase}${path}`;
  }

  function absoluteAudioUrl(url: string) {
    if (!url) return "";
    if (/^https?:\/\//i.test(url) || url.startsWith("data:")) return url;
    return `${props.apiBase}${url.startsWith("/") ? url : `/${url}`}`;
  }

  function upsertAudioRecord(record: AudioRecord) {
    setAudioRecords((items) => [record, ...items.filter((item) => item.id !== record.id)]);
    setRecords((items) => [
      { name: record.name, type: record.source === "upload" ? "上传音频" : "合成音频", content: record.text || "" },
      ...items.filter((item) => item.name !== record.name),
    ]);
  }

  function playNodeAudio(url = nodeAudioUrl) {
    const source = absoluteAudioUrl(url);
    if (!source) return;
    const audio = new Audio(source);
    audio.play().catch(() => undefined);
  }

  async function loadNodeAudioRecords(source: "all" | "upload" | "synthesis" = nodeRecordSource) {
    setAudioBusy(true);
    setNodeAudioMessage("");
    try {
      const response = await fetch(audioApiUrl(`/api/dialogue/audio/records?source=${encodeURIComponent(source)}`));
      if (!response.ok) throw new Error(await response.text());
      const data = await response.json() as AudioRecord[];
      setAudioRecords(data);
      if (!data.length) setNodeAudioMessage("暂无可选录音，请先本地上传或在线试听生成合成音频。");
      return data;
    } catch (error) {
      setNodeAudioMessage(error instanceof Error ? error.message : "录音列表加载失败");
      return [];
    } finally {
      setAudioBusy(false);
    }
  }

  function readFileAsDataUrl(file: File) {
    return new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(new Error("录音文件读取失败"));
      reader.readAsDataURL(file);
    });
  }

  async function uploadNodeAudio(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    const isWav = file.name.toLowerCase().endsWith(".wav") || file.type.includes("wav");
    if (!isWav) {
      openNotice("本地上传仅支持 wav 录音文件。");
      event.target.value = "";
      return;
    }
    if (file.size > 10 * 1024 * 1024) {
      openNotice("录音文件不能超过 10MB。");
      event.target.value = "";
      return;
    }
    setAudioBusy(true);
    setNodeAudioMessage("");
    try {
      const dataUrl = await readFileAsDataUrl(file);
      const response = await fetch(audioApiUrl("/api/dialogue/audio/upload"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: file.name, content_type: file.type || "audio/wav", text: nodePromptDraft, data_url: dataUrl }),
      });
      if (!response.ok) throw new Error(await response.text());
      const record = await response.json() as AudioRecord;
      setNodeAudioSource("upload");
      setNodeRecordSource("upload");
      setNodeSelectedAudioId(record.id);
      setNodeAudioUrl(record.audio_url);
      setNodeAudioName(record.name);
      setNodeAudioMessage("本地录音已上传并可试听。");
      upsertAudioRecord(record);
    } catch (error) {
      openNotice(error instanceof Error ? error.message : "录音上传失败");
    } finally {
      setAudioBusy(false);
    }
  }

  async function auditionNodePrompt() {
    const text = nodePromptDraft.trim();
    if (!text) {
      openNotice("AI话术不能为空");
      return;
    }
    setAudioBusy(true);
    setNodeAudioMessage("");
    try {
      const response = await fetch(audioApiUrl("/api/dialogue/audio/audition"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...props.aiModelConfig, text }),
      });
      if (!response.ok) throw new Error(await response.text());
      const record = await response.json() as AudioRecord;
      setNodeAudioSource("recording");
      setNodeRecordSource("synthesis");
      setNodeSelectedAudioId(record.id);
      setNodeAudioUrl(record.audio_url);
      setNodeAudioName(record.name);
      setNodeAudioMessage("合成试听音频已生成，可在录音选择中复用。");
      upsertAudioRecord(record);
      playNodeAudio(record.audio_url);
    } catch (error) {
      openNotice(error instanceof Error ? error.message : "在线试听失败，请检查后端 TTS 配置。");
    } finally {
      setAudioBusy(false);
    }
  }

  function chooseNodeAudioRecord(id: string) {
    setNodeSelectedAudioId(id);
    const record = audioRecords.find((item) => item.id === id);
    if (!record) return;
    setNodeAudioUrl(record.audio_url);
    setNodeAudioName(record.name);
    setNodePromptDraft(record.text || nodePromptDraft);
    setNodeAudioSource("recording");
    setNodeRecordSource(record.source === "upload" ? "upload" : "synthesis");
    setNodeAudioMessage("已选择录音，AI话术按录音文本回填并锁定。");
  }

  function nodeHasScriptText(node: ScriptNode) {
    return node.kind !== "flow" || Boolean((node.prompt || "").trim());
  }

  function nodeReadyForConnection(node: ScriptNode) {
    if (node.kind === "flow") {
      return Boolean((node.prompt || "").trim()) && Boolean(node.branches?.length);
    }
    const nextStep = node.nextStep || "";
    if (!nextStep && !node.target && !(node.prompt || "").trim()) return false;
    if (nextStep.includes("指定") && !node.target) return false;
    return true;
  }

  function wouldCreateConnectionLoop(sourceId: number, targetId: number) {
    if (sourceId === targetId) return true;
    const visit = (nodeId: number, visited = new Set<number>()): boolean => {
      if (nodeId === sourceId) return true;
      if (visited.has(nodeId)) return false;
      visited.add(nodeId);
      const node = canvasNodeMap.get(nodeId);
      if (!node?.routes) return false;
      return Object.values(node.routes).some((nextId) => visit(nextId, visited));
    };
    return visit(targetId);
  }

  function validateConnection(sourceId: number, branch: string, targetId: number) {
    const source = canvasNodeMap.get(sourceId);
    const target = canvasNodeMap.get(targetId);
    if (!source || !target) return "请拖动连接到有效节点。";
    if (source.kind !== "flow") return "跳转节点不能作为连线起点。";
    if (sourceId === targetId) return "节点不能连接到自身。";
    if (visibleRouteTarget(source, branch)) return "该用户回答已连接，请先删除原有连线。";
    if (!nodeHasScriptText(source)) return "AI话术不能为空";
    if (!nodeReadyForConnection(target)) return "请双击新建成功保存节点后再连接，否则无效。";
    if (wouldCreateConnectionLoop(sourceId, targetId)) return "不能连接到当前节点的上级节点。";
    return "";
  }

  function validateCanvasNodeForm(kind: ScriptNode["kind"], data: Record<string, FormDataEntryValue>, branches: string[]) {
    if (!String(data.name || "").trim()) return "节点名称不能为空";
    if (kind === "flow") {
      if (!String(data.prompt || "").trim()) return "AI话术不能为空";
      if (!branches.length) return "流程节点必须有子节点。";
    }
    if (kind === "jump") {
      const nextStep = String(data.nextStep || "");
      if (!nextStep) return "下一步必须选择。";
      if (nextStep.includes("指定") && !String(data.target || "").trim()) {
        return "下一步是指定主流程，则必须选择要跳转的流程节点。";
      }
    }
    return "";
  }

  function validateFlowBeforeSave() {
    const visibleCanvasNodes = stripInvisibleRoutes(canvasNodes);
    const visibleCanvasNodeMap = new Map(visibleCanvasNodes.map((node) => [node.id, node]));
    if (!visibleCanvasNodes.length) return "请先添加流程节点或跳转节点。";
    for (const node of visibleCanvasNodes) {
      if (!nodeReadyForConnection(node)) {
        return node.kind === "flow" ? "AI话术不能为空" : "下一步是指定主流程，则必须选择要跳转的流程节点。";
      }
    }
    for (const node of visibleCanvasNodes) {
      if (node.kind !== "flow") continue;
      const branches = node.branches || [];
      const visibleRoutes = Object.fromEntries(Object.entries(node.routes || {}).filter(([, targetId]) => visibleCanvasNodeMap.has(targetId)));
      if (!branches.length || Object.keys(visibleRoutes).length === 0) {
        return "流程节点必须有子节点。";
      }
      const missingBranch = branches.find((branch) => !visibleRoutes[branch]);
      if (missingBranch) {
        return missingBranchMessage(node, missingBranch);
      }
    }
    const incomingIds = new Set<number>();
    visibleCanvasNodes.forEach((node) => Object.values(node.routes || {}).forEach((targetId) => incomingIds.add(targetId)));
    const rootFlowCount = visibleCanvasNodes.filter((node) => node.kind === "flow" && !incomingIds.has(node.id)).length;
    if (rootFlowCount > 1) return "每个场景节点只能有一个根节点。";
    return "";
  }

  async function saveScript(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.currentTarget));
    const typeValue = String(data.is_variable || " ");
    if (typeValue === " ") {
      window.alert("请选择话术类型");
      return;
    }
    const industryMap: Record<string, string> = {
      "0": "请选择行业",
      "1": "金融",
      "2": "贷款",
      "3": "房产",
      "4": "装修",
      "5": "汽车",
      "6": "教育",
      "7": "其他",
    };
    const nextType = typeValue === "1" ? "variable" : "common";
    const saved = await props.onSaveScriptScene({
      name: String(data.name || "新建话术"),
      industry: industryMap[String(data.tradeType || "0")] || "请选择行业",
      business_type: "",
      script_type: nextType,
      auto_break: String(data.break || "1") === "0" ? "是" : "否",
      audit_status: editingCard?.audit || "待审核",
      status: "draft",
      ui: editingCard?.persisted ? { source_scene_id: editingCard.id } : {},
    });
    const next: ScriptCard = {
      id: saved.id,
      name: String(data.name || "新建话术"),
      industry: industryMap[String(data.tradeType || "0")] || "请选择行业",
      audit: editingCard?.audit || "待审核",
      updated: new Date().toLocaleString("zh-CN", { hour12: false }).replace(/\//g, "-"),
      status: editingCard?.status || "异常",
      type: nextType,
      autoBreak: String(data.break || "1") === "0" ? "是" : "否",
      persisted: true,
    };
    if (editingScriptIndex === null) {
      setScriptCards((items) => [next, ...items]);
      setActiveScript(0);
      setScriptType(nextType);
      setActiveTab("流程");
    } else {
      setScriptCards((items) => items.map((item, index) => index === editingScriptIndex ? next : item));
      setActiveScript(editingScriptIndex);
      setScriptType(nextType);
      setActiveTab("流程");
    }
    setEditingScriptIndex(null);
    setModal(null);
  }

  async function copyScript(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.currentTarget));
    const saved = await props.onSaveScriptScene({
      name: String(data.name || `${activeCard.name}-复制`),
      industry: activeCard.industry,
      business_type: "",
      script_type: activeCard.type,
      auto_break: activeCard.autoBreak || "否",
      audit_status: "待审核",
      status: "draft",
      ui: {},
    });
    const flow = buildDialogueFlow();
    await props.onSaveScriptFlow(saved.id, {
      name: saved.name,
      industry: saved.industry,
      script_type: saved.script_type || activeCard.type,
      auto_break: saved.auto_break || activeCard.autoBreak || "否",
      audit_status: "待审核",
      status: "draft",
      flow,
      ui: flow.ui as Record<string, unknown>,
    });
    const copy: ScriptCard = {
      ...activeCard,
      id: saved.id,
      name: saved.name,
      audit: "待审核",
      updated: saved.updated_at || new Date().toLocaleString("zh-CN", { hour12: false }).replace(/\//g, "-"),
      status: "异常",
      persisted: true,
    };
    setScriptCards((items) => [copy, ...items]);
    setActiveScript(0);
    setModal(null);
  }

  async function addSceneNode(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!activeCard?.persisted) {
      openNotice("请先新建并保存话术，再添加场景节点。");
      return;
    }
    const data = Object.fromEntries(new FormData(event.currentTarget));
    const node: SceneNode = {
      id: Date.now(),
      scriptId: activeCard.id,
      name: String(data.flowname || data.name || "场景节点"),
      group: data.scenetype === "1" || data.group === "public" ? "public" : "normal",
    };
    const nextSceneNodes = [...activeSceneNodes, node];
    const scriptSceneIds = new Set(activeSceneNodes.map((item) => item.id));
    const scriptCanvasNodes = nodes.filter((item) => scriptSceneIds.has(item.sceneId));
    setSceneNodes((items) => [...items, node]);
    setActiveSceneId(node.id);
    setModal(null);
    await props.onSaveScriptUi(activeCard.id, {
      scene_nodes: nextSceneNodes,
      canvas_nodes: scriptCanvasNodes,
      variables: currentVariables,
    });
  }

  function saveCanvasNode(kind: ScriptNode["kind"], event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!activeSceneId) {
      openNotice("请先添加场景节点，再添加流程节点或跳转节点。");
      return;
    }
    const formData = new FormData(event.currentTarget);
    const data = Object.fromEntries(formData);
    const selectedBranches = formData.getAll("branches").map((item) => String(item).trim()).filter(Boolean);
    const branches = kind === "flow"
      ? selectedBranches
      : String(data.branches || "否定,拒绝,肯定,中性,未识别").split(/[,，\n]/).map((item) => item.trim()).filter(Boolean);
    const formError = validateCanvasNodeForm(kind, data, branches);
    if (formError) {
      openNotice(formError);
      return;
    }
    const currentRuleDrafts = editingBranchRule
      ? { ...nodeIntentKeywordDrafts, [branchToRouteKey(editingBranchRule)]: splitRuleKeywords(branchRuleInput) }
      : nodeIntentKeywordDrafts;
    const intentKeywords = kind === "flow" ? cleanIntentKeywordsForBranches(branches, currentRuleDrafts) : undefined;
    const audioFields = {
      audioSource: String(data.audioSource || "upload") as ScriptNode["audioSource"],
      audioRecordSource: String(data.audioRecordSource || "all") as ScriptNode["audioRecordSource"],
      audioRecordId: String(data.audioRecordId || ""),
      audioUrl: String(data.audioUrl || ""),
      audioName: String(data.audioName || ""),
      audioText: String(data.prompt || ""),
    };
    if (editingNode) {
      setNodes((items) => items.map((item) => item.id === editingNode.id ? {
        ...item,
        name: String(data.name || item.name),
        prompt: String(data.prompt || ""),
        label: String(data.label || ""),
        nextStep: String(data.nextStep || ""),
        target: String(data.target || ""),
        pauseMs: Number(data.pauseMs || item.pauseMs || 1000),
        ...audioFields,
        branches: kind === "flow" ? branches : item.branches,
        intentKeywords: kind === "flow" ? intentKeywords : item.intentKeywords,
        routes: kind === "flow" ? Object.fromEntries(Object.entries(item.routes || {}).filter(([branch]) => branches.includes(branch))) : item.routes,
      } : item));
      setEditingNodeId(null);
      setModal(null);
      return;
    }
    const index = canvasNodes.length;
    const node: ScriptNode = {
      id: Date.now(),
      kind,
      sceneId: activeSceneId,
      name: String(data.name || (kind === "flow" ? "流程节点" : "跳转节点")),
      prompt: String(data.prompt || ""),
      label: String(data.label || ""),
      nextStep: String(data.nextStep || ""),
      target: String(data.target || ""),
      pauseMs: Number(data.pauseMs || (kind === "jump" ? 3000 : 10000)),
      ...audioFields,
      branches: kind === "flow" ? branches : undefined,
      intentKeywords: kind === "flow" ? intentKeywords : undefined,
      routes: kind === "flow" ? {} : undefined,
      x: kind === "flow" ? 40 + (index % 2) * 260 : 170 + (index % 2) * 260,
      y: kind === "flow" ? 60 + Math.floor(index / 2) * 160 : 330 + Math.floor(index / 2) * 160,
    };
    setNodes((items) => [...items, node]);
    setEditingNodeId(null);
    setModal(null);
  }

  function buildCanvasNode(kind: ScriptNode["kind"], x: number, y: number): ScriptNode {
    return {
      id: Date.now(),
      kind,
      sceneId: activeSceneId,
      name: kind === "flow" ? "流程节点" : "跳转节点",
      prompt: "",
      label: "",
      nextStep: kind === "jump" ? "指定主动流程" : "",
      target: kind === "jump" ? activeSceneNodes.find((node) => node.group === "public")?.name || "" : "",
      pauseMs: kind === "jump" ? 3000 : 10000,
      branches: kind === "flow" ? defaultBranches : undefined,
      intentKeywords: kind === "flow" ? {} : undefined,
      routes: kind === "flow" ? {} : undefined,
      audioSource: "upload",
      audioRecordSource: "all",
      audioRecordId: "",
      audioUrl: "",
      audioName: "",
      audioText: "",
      x: Math.max(10, x),
      y: Math.max(10, y),
    };
  }

  function dragToolbarNode(kind: ScriptNode["kind"], event: DragEvent<HTMLButtonElement>) {
    event.dataTransfer.effectAllowed = "copy";
    event.dataTransfer.setData("application/x-script-node-kind", kind);
  }

  function dragCanvasNode(nodeId: number, event: DragEvent<HTMLElement>) {
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("application/x-script-node-id", String(nodeId));
  }

  function dropOnCanvas(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    const canvas = event.currentTarget;
    const rect = canvas.getBoundingClientRect();
    const x = event.clientX - rect.left + canvas.scrollLeft - 146;
    const y = event.clientY - rect.top + canvas.scrollTop - 26;
    const movingNodeId = Number(event.dataTransfer.getData("application/x-script-node-id"));
    if (movingNodeId) {
      setNodes((items) => items.map((item) => item.id === movingNodeId ? { ...item, x: Math.max(10, x), y: Math.max(10, y) } : item));
      return;
    }
    const kind = event.dataTransfer.getData("application/x-script-node-kind") as ScriptNode["kind"];
    if (kind === "flow" || kind === "jump") {
      if (!activeSceneId) {
        openNotice("请先添加场景节点，再拖入流程节点或跳转节点。");
        return;
      }
      setNodes((items) => [...items, buildCanvasNode(kind, x, y)]);
    }
  }

  function selectScript(index: number) {
    setActiveScript(index);
    setActiveTab("流程");
    setEditingNodeId(null);
    setModal(null);
  }

  function saveVariable(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.currentTarget));
    const name = String(data.variable_name || "").trim();
    const annotation = String(data.annotation || "").trim();
    const example = String(data.example || "").trim();
    if (!name) {
      window.alert("变量名称不能为空");
      return;
    }
    if (!annotation) {
      window.alert("变量标识不能为空");
      return;
    }
    if (!example) {
      window.alert("变量示例不能为空");
      return;
    }
    if (editingVariable) {
      setVariables((items) => items.map((item) => item.id === editingVariable.id ? { ...item, name, annotation, example } : item));
    } else {
      setVariables((items) => [...items, { id: Date.now(), scriptId: activeCard.id, name, annotation, example }]);
    }
    setEditingVariableId(null);
    setSelectedVariableIds([]);
    setModal(null);
  }

  function toggleVariableSelection(id: number, checked: boolean) {
    setSelectedVariableIds((items) => checked ? Array.from(new Set([...items, id])) : items.filter((item) => item !== id));
  }

  function deleteVariable(id: number) {
    setVariables((items) => items.filter((item) => item.id !== id));
    setSelectedVariableIds((items) => items.filter((item) => item !== id));
  }

  function deleteSelectedVariables() {
    if (!selectedVariableIds.length) {
      openNotice("请至少选择一条变量。");
      return;
    }
    setVariables((items) => items.filter((item) => !selectedVariableIds.includes(item.id)));
    setSelectedVariableIds([]);
    openNotice("变量已删除。");
  }

  function addBranchCategory() {
    const name = newBranchName.trim();
    if (!name) return;
    if (nodeBranchDrafts.includes(name)) {
      window.alert("该类别已存在。");
      return;
    }
    setNodeBranchDrafts((items) => [...items, name]);
    setNewBranchName("");
    setEditingBranchRule(name);
    setBranchRuleInput("");
  }

  function removeBranchCategory(branch: string) {
    if (defaultBranches.includes(branch)) return;
    const key = branchToRouteKey(branch);
    setNodeBranchDrafts((items) => items.filter((item) => item !== branch));
    setNodeIntentKeywordDrafts((items) => {
      const next = { ...items };
      delete next[key];
      delete next[branch];
      return next;
    });
    if (editingBranchRule === branch) {
      setEditingBranchRule(null);
      setBranchRuleInput("");
    }
  }

  function openBranchRuleEditor(branch: string) {
    setEditingBranchRule(branch);
    setBranchRuleInput(keywordsForBranch(branch).join("\n"));
  }

  function saveBranchRule() {
    if (!editingBranchRule) return;
    const key = branchToRouteKey(editingBranchRule);
    const keywords = Array.from(new Set(splitRuleKeywords(branchRuleInput)));
    setNodeIntentKeywordDrafts((items) => ({ ...items, [key]: keywords }));
    setEditingBranchRule(null);
    setBranchRuleInput("");
  }

  function branchTone(branch: string) {
    if (branch.includes("肯定")) return "tone-positive";
    if (branch.includes("拒绝")) return "tone-reject";
    if (branch.includes("否定")) return "tone-negative";
    if (branch.includes("中性")) return "tone-neutral";
    if (branch.includes("未识别")) return "tone-unknown";
    return "tone-default";
  }

  function branchMarker(branch: string) {
    return `url(#canvas-arrow-${branchTone(branch).replace("tone-", "")})`;
  }

  function branchListClass(branches: string[] = []) {
    const count = Math.max(1, Math.min(5, branches.length || 1));
    return `branch-count-${count}`;
  }

  function branchPoint(node: ScriptNode, branch: string) {
    const measured = endpointCenters[`branch-${node.id}-${branch}`];
    if (measured) return measured;
    const branches = node.branches || [];
    const index = Math.max(0, branches.indexOf(branch));
    const maxColumns = 4;
    const columnCount = Math.max(1, Math.min(maxColumns, branches.length || 1));
    const row = Math.floor(index / maxColumns);
    const col = index % maxColumns;
    const remainingInRow = branches.length - row * maxColumns;
    const itemsInRow = Math.max(1, Math.min(maxColumns, remainingInRow));
    const availableWidth = 272;
    const gap = 8;
    const columnWidth = (availableWidth - gap * (columnCount - 1)) / columnCount;
    const rowWidth = itemsInRow * columnWidth + gap * (itemsInRow - 1);
    const startX = branches.length > maxColumns && row > 0
      ? 10 + (availableWidth - rowWidth) / 2
      : 10;
    return {
      x: node.x + startX + col * (columnWidth + gap) + columnWidth / 2,
      y: node.y + 144 + row * 40,
    };
  }

  function nodeInputPoint(node: ScriptNode) {
    const measured = endpointCenters[`node-${node.id}`];
    if (measured) return measured;
    return {
      x: node.x + 146,
      y: node.y - 1,
    };
  }

  function routePath(points: Array<{ x: number; y: number }>, cornerRadius = 5) {
    if (points.length < 2) return "";
    const commands = [`M ${points[0].x} ${points[0].y}`];
    for (let index = 1; index < points.length; index += 1) {
      const previous = points[index - 1];
      const current = points[index];
      const next = points[index + 1];
      if (!next || cornerRadius <= 0) {
        commands.push(`L ${current.x} ${current.y}`);
        continue;
      }
      const previousLength = Math.hypot(current.x - previous.x, current.y - previous.y);
      const nextLength = Math.hypot(next.x - current.x, next.y - current.y);
      const radius = Math.min(cornerRadius, previousLength / 2, nextLength / 2);
      if (!radius || previousLength === 0 || nextLength === 0) {
        commands.push(`L ${current.x} ${current.y}`);
        continue;
      }
      const before = {
        x: current.x - ((current.x - previous.x) / previousLength) * radius,
        y: current.y - ((current.y - previous.y) / previousLength) * radius,
      };
      const after = {
        x: current.x + ((next.x - current.x) / nextLength) * radius,
        y: current.y + ((next.y - current.y) / nextLength) * radius,
      };
      commands.push(`L ${before.x} ${before.y}`);
      commands.push(`Q ${current.x} ${current.y} ${after.x} ${after.y}`);
    }
    return commands.join(" ");
  }

  function routeArrowAt(points: Array<{ x: number; y: number }>, location: number) {
    const segments = points.flatMap((point, index) => {
      const next = points[index + 1];
      if (!next) return [];
      return [{
        from: point,
        to: next,
        length: Math.hypot(next.x - point.x, next.y - point.y),
      }];
    });
    const total = segments.reduce((sum, segment) => sum + segment.length, 0);
    let remaining = total * location;
    for (const segment of segments) {
      if (remaining > segment.length) {
        remaining -= segment.length;
        continue;
      }
      const ratio = segment.length ? remaining / segment.length : 0;
      return {
        x: segment.from.x + (segment.to.x - segment.from.x) * ratio,
        y: segment.from.y + (segment.to.y - segment.from.y) * ratio,
        angle: Math.atan2(segment.to.y - segment.from.y, segment.to.x - segment.from.x) * 180 / Math.PI,
      };
    }
    const last = points[points.length - 1] || { x: 0, y: 0 };
    return { x: last.x, y: last.y, angle: 0 };
  }

  function routeArrows(points: Array<{ x: number; y: number }>) {
    return [0.2, 0.7].map((location) => routeArrowAt(points, location));
  }

  function connectionRoute(sourceNode: ScriptNode, branch: string, target: { x: number; y: number }) {
    const source = branchPoint(sourceNode, branch);
    const stub = 50;
    const middleX = source.x + (target.x - source.x) / 2;
    const points = [
      source,
      { x: source.x, y: source.y + stub },
      { x: middleX, y: source.y + stub },
      { x: middleX, y: target.y - stub },
      { x: target.x, y: target.y - stub },
      target,
    ];
    return {
      d: routePath(points),
      arrows: routeArrows(points),
    };
  }

  function beginBranchConnection(nodeId: number, branch: string, event: PointerEvent<HTMLButtonElement>) {
    event.preventDefault();
    event.stopPropagation();
    const source = canvasNodeMap.get(nodeId);
    if (!source || !nodeHasScriptText(source)) {
      openNotice("AI话术不能为空");
      return;
    }
    if (visibleRouteTarget(source, branch)) {
      openNotice("该用户回答已连接，请先删除原有连线。");
      return;
    }
    const canvas = event.currentTarget.closest(".flow-canvas-replica") as HTMLDivElement | null;
    const rect = canvas?.getBoundingClientRect();
    setConnectingFrom({
      nodeId,
      branch,
      x: rect ? event.clientX - rect.left + (canvas?.scrollLeft || 0) : 0,
      y: rect ? event.clientY - rect.top + (canvas?.scrollTop || 0) : 0,
    });
  }

  function updateBranchConnection(event: PointerEvent<HTMLDivElement>) {
    if (!connectingFrom) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const x = event.clientX - rect.left + event.currentTarget.scrollLeft;
    const y = event.clientY - rect.top + event.currentTarget.scrollTop;
    setConnectingFrom((item) => item ? {
      ...item,
      x,
      y,
    } : null);
  }

  function finishBranchConnection(event: PointerEvent<HTMLDivElement>) {
    if (!connectingFrom) return;
    const target = (event.target as HTMLElement).closest("[data-connect-target]") as HTMLElement | null;
    const targetId = Number(target?.dataset.connectTarget || 0);
    if (targetId) {
      const error = validateConnection(connectingFrom.nodeId, connectingFrom.branch, targetId);
      if (error) {
        openNotice(error);
        setConnectingFrom(null);
        return;
      }
      setNodes((items) => items.map((item) => item.id === connectingFrom.nodeId ? {
        ...item,
        routes: { ...(item.routes || {}), [connectingFrom.branch]: targetId },
      } : item));
    }
    setConnectingFrom(null);
  }

  function deleteConnection(sourceId: number, branch: string) {
    if (!window.confirm("确定删除所点击的链接吗？")) return;
    setNodes((items) => items.map((item) => {
      if (item.id !== sourceId) return item;
      const nextRoutes = { ...(item.routes || {}) };
      delete nextRoutes[branch];
      return { ...item, routes: nextRoutes };
    }));
  }

  function removeNode(id: number) {
    setNodes((items) => items
      .filter((item) => item.id !== id)
      .map((item) => {
        if (!item.routes) return item;
        return { ...item, routes: Object.fromEntries(Object.entries(item.routes).filter(([, targetId]) => targetId !== id)) };
      }));
  }

  function buildDialogueFlow() {
    type BackendFlowNode = {
      id: string;
      type: string;
      name: string;
      text: string;
      ui: Record<string, unknown>;
      routes?: Record<string, string>;
      intent_keywords?: Record<string, unknown>;
    };
    const scriptSceneIds = new Set(activeSceneNodes.map((node) => node.id));
    const scriptNodes = stripInvisibleRoutes(nodes.filter((node) => scriptSceneIds.has(node.sceneId)));
    const scriptNodeIdsByScene = new Map<number, Set<number>>();
    scriptNodes.forEach((node) => {
      const ids = scriptNodeIdsByScene.get(node.sceneId) || new Set<number>();
      ids.add(node.id);
      scriptNodeIdsByScene.set(node.sceneId, ids);
    });
    const idOf = (id: number) => `node-${id}`;
    const incomingIds = new Set<number>();
    scriptNodes.forEach((node) => Object.values(node.routes || {}).forEach((targetId) => incomingIds.add(targetId)));
    const root = scriptNodes.find((node) => node.kind === "flow" && !incomingIds.has(node.id)) || scriptNodes.find((node) => node.kind === "flow") || scriptNodes[0];
    const flowNodes: BackendFlowNode[] = scriptNodes.map((node) => {
      const base = {
        id: idOf(node.id),
        type: node.kind === "jump" ? "end" : "scene",
        name: node.name,
        text: node.kind === "jump" ? (node.prompt || node.target || node.nextStep || node.name) : node.prompt,
        ui: {
          kind: node.kind,
          sceneId: node.sceneId,
          x: node.x,
          y: node.y,
          label: node.label || "",
          nextStep: node.nextStep || "",
          target: node.target || "",
          pauseMs: node.pauseMs || 10000,
          branches: node.branches || [],
          intentKeywords: node.intentKeywords || {},
          audioSource: node.audioSource || "upload",
          audioRecordSource: node.audioRecordSource || "all",
          audioRecordId: node.audioRecordId || "",
          audioUrl: node.audioUrl || "",
          audioName: node.audioName || "",
          audioText: node.audioText || node.prompt || "",
        },
      };
      if (node.kind !== "flow") return base;
      return {
        ...base,
        routes: Object.fromEntries(Object.entries(node.routes || {}).filter(([, targetId]) => (
          scriptNodeIdsByScene.get(node.sceneId)?.has(targetId)
        )).map(([branch, targetId]) => [
          branchToRouteKey(branch),
          idOf(targetId),
        ])),
        intent_keywords: cleanIntentKeywordsForBranches(node.branches || [], node.intentKeywords || {}),
      };
    });
    if (!flowNodes.length) {
      flowNodes.push({ id: "fallback", type: "llm_fallback", name: "LLM兜底", text: "", ui: {} });
    }
    if (!flowNodes.some((node) => node.id === "fallback")) {
      flowNodes.push({ id: "fallback", type: "llm_fallback", name: "LLM兜底", text: "", ui: {} });
    }
    return {
      entry_node: root ? idOf(root.id) : flowNodes[0].id,
      max_turns: 10,
      unknown_route: "fallback",
      nodes: flowNodes,
      ui: {
        scene_nodes: activeSceneNodes,
        canvas_nodes: scriptNodes,
        variables: currentVariables,
      },
    };
  }

  async function saveAll() {
    const flowError = validateFlowBeforeSave();
    if (flowError) {
      openNotice(flowError);
      return;
    }
    if (!activeCard?.persisted) {
      openNotice("请先新建并保存话术，再保存流程。");
      return;
    }
    const flow = buildDialogueFlow();
    await props.onSaveScriptFlow(activeCard.id, {
      name: activeCard.name,
      industry: activeCard.industry,
      script_type: activeCard.type,
      auto_break: activeCard.autoBreak || "否",
      audit_status: activeCard.audit,
      status: activeCard.status === "正常" ? "published" : "draft",
      flow,
      ui: flow.ui as Record<string, unknown>,
    });
    setSavedAt(new Date().toLocaleTimeString("zh-CN", { hour12: false }));
    openNotice("话术配置已保存到后端数据库。");
  }

  async function dispatchScript(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.currentTarget));
    if (!activeCard?.persisted) {
      openNotice("请先新建并保存话术，再进行话术下发。");
      return;
    }
    const flowError = validateFlowBeforeSave();
    if (flowError) {
      openNotice(flowError);
      return;
    }
    try {
      const flow = buildDialogueFlow();
      await props.onSaveScriptFlow(activeCard.id, {
        name: activeCard.name,
        industry: activeCard.industry,
        script_type: activeCard.type,
        auto_break: activeCard.autoBreak || "否",
        audit_status: activeCard.audit,
        status: "draft",
        flow,
        ui: flow.ui as Record<string, unknown>,
      });
      await props.onPublish(activeCard.id);
      if (String(data.target || "all") === "all") {
        await props.onSetDefault(activeCard.id);
        setScriptCards((items) => items.map((item) => item.id === activeCard.id ? { ...item, status: "正常", audit: "审核通过", persisted: true } : item));
        openNotice("话术已下发到全部机器人，并设为后台生效话术。");
      } else {
        openNotice("话术已发布。指定机器人下发需要接入机器人实例列表后再选择目标。");
      }
    } catch (error) {
      openNotice(error instanceof Error ? error.message : "话术下发失败，请检查流程配置。");
    }
  }

  function scriptWorkspaceNodes() {
    const sceneIds = new Set(activeSceneNodes.map((node) => node.id));
    return nodes.filter((node) => sceneIds.has(node.sceneId));
  }

  function backendFlowSimulationNodes() {
    if (!props.scene || props.scene.id !== activeCard?.id || !props.scene.flow?.nodes?.length) return [];
    const fallbackSceneId = activeSceneNodes[0]?.id || activeCard.id * 1000 + 1;
    const idMap = new Map(props.scene.flow.nodes.map((node) => [node.id, stableNodeId(activeCard.id, node.id)]));
    return props.scene.flow.nodes
      .filter((node) => node.type !== "llm_fallback")
      .map((node) => {
        const uiNode = (node.ui || {}) as Partial<ScriptNode>;
        const id = idMap.get(node.id) || stableNodeId(activeCard.id, node.id);
        const routes = node.routes || {};
        return {
          id,
          name: node.name || (node.type === "end" ? "跳转节点" : "流程节点"),
          kind: node.type === "end" ? "jump" : "flow",
          sceneId: Number(uiNode.sceneId || fallbackSceneId),
          prompt: node.text || "",
          label: String(uiNode.label || ""),
          nextStep: String(uiNode.nextStep || ""),
          target: String(uiNode.target || ""),
          pauseMs: Number(uiNode.pauseMs || 10000),
          branches: node.type === "scene" ? Object.keys(routes).map(routeKeyToBranch) : undefined,
          intentKeywords: node.type === "scene" ? normalizeIntentKeywords(node.intent_keywords || uiNode.intentKeywords || {}) : undefined,
          routes: node.type === "scene"
            ? Object.fromEntries(Object.entries(routes).map(([route, target]) => [routeKeyToBranch(route), idMap.get(target)]).filter(([, target]) => target))
            : undefined,
          x: Number(uiNode.x || 0),
          y: Number(uiNode.y || 0),
        } as ScriptNode;
      });
  }

  function simulationNodes() {
    const workspaceNodes = scriptWorkspaceNodes();
    return workspaceNodes.some((node) => node.kind === "flow") ? workspaceNodes : backendFlowSimulationNodes();
  }

  function simulationEntryNode() {
    const workspaceNodes = simulationNodes();
    const scopedNodes = canvasNodes.some((node) => node.kind === "flow") ? canvasNodes : workspaceNodes;
    const incomingIds = new Set<number>();
    scopedNodes.forEach((node) => Object.values(node.routes || {}).forEach((targetId) => incomingIds.add(targetId)));
    return scopedNodes.find((node) => node.kind === "flow" && !incomingIds.has(node.id))
      || scopedNodes.find((node) => node.kind === "flow")
      || null;
  }

  function branchFromReply(reply: string, currentNode: ScriptNode) {
    const branches = currentNode.branches || [];
    const text = reply.trim();
    const direct = branches.find((branch) => text.includes(branch));
    if (direct) return direct;
    const normalized = text.toLowerCase();
    const customBranch = branches.find((branch) => keywordsForBranch(branch, currentNode.intentKeywords || {}).some((keyword) => normalized.includes(keyword.toLowerCase())));
    if (customBranch) return customBranch;
    const branchByIntent = [
      { branch: "拒绝", keywords: ["拒绝", "别打", "不要打", "挂了", "拉黑", "投诉"] },
      { branch: "否定", keywords: ["否", "不是", "不用", "不需要", "没兴趣", "不方便", "算了"] },
      { branch: "肯定", keywords: ["肯定", "是", "对", "可以", "方便", "好", "行", "了解", "有兴趣"] },
      { branch: "中性", keywords: ["中性", "什么", "多少", "哪里", "介绍", "怎么", "讲讲"] },
    ].find((item) => item.keywords.some((keyword) => normalized.includes(keyword)));
    if (branchByIntent) {
      const matched = branches.find((branch) => branch.includes(branchByIntent.branch));
      if (matched) return matched;
    }
    return branches.find((branch) => branch.includes("未识别")) || branches[0] || "";
  }

  function startSimulation() {
    const entry = simulationEntryNode();
    setChatInput("");
    if (!entry) {
      setSimulateNodeId(null);
      setChat([{ role: "ai", text: "当前话术没有可测试的流程节点，请先添加流程节点并配置话术内容。" }]);
      setModal("simulate");
      return;
    }
    setSimulateNodeId(entry.id);
    setChat([{ role: "ai", text: entry.prompt || entry.name }]);
    setModal("simulate");
  }

  function checkScript() {
    const flowError = validateFlowBeforeSave();
    if (flowError) {
      openNotice(flowError);
      return;
    }
    setScriptCards((items) => items.map((item, index) => index === activeScript ? { ...item, status: "正常", audit: "审核通过" } : item));
    openNotice("话术检测完成：流程配置可用。");
  }

  function sendChat(event?: FormEvent<HTMLFormElement>) {
    event?.preventDefault();
    const content = chatInput.trim();
    if (!content) return;
    const workspaceNodes = simulationNodes();
    const currentNode = workspaceNodes.find((node) => node.id === simulateNodeId);
    if (!currentNode || currentNode.kind !== "flow") {
      setChat((items) => [...items, { role: "user", text: content }, { role: "ai", text: "当前节点已经结束，请点击重新开始。" }]);
      setChatInput("");
      return;
    }
    const branch = branchFromReply(content, currentNode);
    const targetId = branch ? currentNode.routes?.[branch] : undefined;
    const target = targetId ? workspaceNodes.find((node) => node.id === targetId) : null;
    const response = target
      ? target.kind === "flow"
        ? target.prompt || target.name
        : target.prompt || target.target || target.nextStep || target.name
      : `分支“${branch || "未识别"}”没有配置下一节点，请回到流程画布完成连线。`;
    setChat((items) => [
      ...items,
      { role: "user", text: content },
      { role: "ai", text: response },
    ]);
    setSimulateNodeId(target?.kind === "flow" ? target.id : null);
    setChatInput("");
  }

  function renderTab() {
    if (activeTab === "流程") {
      return (
        <div className="script-work">
          <div className="flow-toolbar">
            <button type="button" onClick={() => setModal("sceneNode")}>添加场景节点</button>
            <button className="drag-tool" draggable title="拖动到画布中创建流程节点" type="button" onDragStart={(event) => dragToolbarNode("flow", event)}>流程节点</button>
            <button className="drag-tool" draggable title="拖动到画布中创建跳转节点" type="button" onDragStart={(event) => dragToolbarNode("jump", event)}>跳转节点</button>
            <button type="button" onClick={saveAll}>保存</button>
            <button type="button" onClick={() => setFullscreen((value) => !value)}>{fullscreen ? "退出全屏" : "全屏"}</button>
          </div>
          <p className="script-save-tip">话术配置完成之后，需要点击保存按钮进行保存{savedAt ? `，最近保存 ${savedAt}` : ""}</p>
          <aside>
            <h3>普通场景节点</h3>
            {normalSceneNodes.length ? normalSceneNodes.map((node) => (
              <span className={node.id === activeSceneId ? "active" : ""} key={node.id} onClick={() => setActiveSceneId(node.id)}>{node.name}</span>
            )) : <span>暂无场景节点</span>}
            <h3>公共场景节点</h3>
            {publicSceneNodes.length ? publicSceneNodes.map((node) => (
              <span className={node.id === activeSceneId ? "active" : ""} key={node.id} onClick={() => setActiveSceneId(node.id)}>{node.name}</span>
            )) : <span>暂无场景节点</span>}
          </aside>
          <div className="flow-canvas-replica" onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = event.dataTransfer.types.includes("application/x-script-node-id") ? "move" : "copy"; }} onDrop={dropOnCanvas} onPointerMove={updateBranchConnection} onPointerUp={finishBranchConnection}>
            {canvasNodes.length ? (
              <svg className="canvas-connections" height="1400" width="1800">
                <defs>
                  {["default", "positive", "reject", "negative", "neutral", "unknown"].map((tone) => (
                    <marker id={`canvas-arrow-${tone}`} key={tone} markerHeight="10" markerUnits="strokeWidth" markerWidth="10" orient="auto" refX="9" refY="5">
                      <path className={`canvas-arrow-head tone-${tone}`} d="M 0 0 L 10 5 L 0 10 z" />
                    </marker>
                  ))}
                  <marker id="canvas-arrow" markerHeight="10" markerUnits="strokeWidth" markerWidth="10" orient="auto" refX="9" refY="5">
                    <path className="canvas-arrow-head tone-default" d="M 0 0 L 10 5 L 0 10 z" />
                  </marker>
                </defs>
                {connectionLines.map(({ source, branch, target }) => {
                  const route = connectionRoute(source, branch, nodeInputPoint(target));
                  return (
                    <g
                      data-line-branch={branch}
                      data-line-source={source.id}
                      data-line-target={target.id}
                      key={`${source.id}-${branch}-${target.id}`}
                      onDoubleClick={(event) => { event.stopPropagation(); deleteConnection(source.id, branch); }}
                    >
                      <path
                        className={`canvas-connection ${branchTone(branch)}`}
                        d={route.d}
                        markerEnd={branchMarker(branch)}
                      />
                      {route.arrows.map((arrow, index) => (
                        <path
                          className={`canvas-inline-arrow ${branchTone(branch)}`}
                          d="M -6 -5 L 5 0 L -6 5 z"
                          key={`${source.id}-${branch}-${target.id}-arrow-${index}`}
                          transform={`translate(${arrow.x} ${arrow.y}) rotate(${arrow.angle})`}
                        />
                      ))}
                    </g>
                  );
                })}
                {connectingFrom ? (
                  (() => {
                    const sourceNode = canvasNodeMap.get(connectingFrom.nodeId) || canvasNodes[0];
                    const route = connectionRoute(sourceNode, connectingFrom.branch, { x: connectingFrom.x, y: connectingFrom.y });
                    return (
                      <g>
                        <path
                          className={`canvas-connection preview ${branchTone(connectingFrom.branch)}`}
                          d={route.d}
                          markerEnd={branchMarker(connectingFrom.branch)}
                        />
                        {route.arrows.map((arrow, index) => (
                          <path
                            className={`canvas-inline-arrow preview ${branchTone(connectingFrom.branch)}`}
                            d="M -6 -5 L 5 0 L -6 5 z"
                            key={`preview-arrow-${index}`}
                            transform={`translate(${arrow.x} ${arrow.y}) rotate(${arrow.angle})`}
                          />
                        ))}
                      </g>
                    );
                  })()
                ) : null}
              </svg>
            ) : null}
            {canvasNodes.length ? canvasNodes.map((node) => (
              <article className={`canvas-node ${node.kind}`} draggable key={node.id} onDragStart={(event) => dragCanvasNode(node.id, event)} onDoubleClick={() => { setEditingNodeId(node.id); setModal(node.kind === "flow" ? "flowNode" : "jumpNode"); }} style={{ left: node.x, top: node.y }}>
                <button aria-label={`连接到${node.name}`} className="node-port-top" data-connect-target={node.id} draggable={false} type="button"></button>
                <strong className="canvas-node-heading">{node.name}</strong>
                <button className="canvas-close" type="button" onClick={() => removeNode(node.id)}>X</button>
                <div className="node-content-row">{node.kind === "flow" ? node.prompt : (node.prompt || "")}</div>
                <div className={node.kind === "flow" ? `choices-list ${branchListClass(node.branches)}` : "jump-next-row"}>
                  {node.kind === "flow" ? (node.branches || []).map((branch) => (
                    <div className={`actionli ${branchTone(branch)} ${visibleRouteTarget(node, branch) ? "connected" : ""}`} key={branch}>
                      <span>{branch}</span>
                      <button aria-label={`从${branch}连线`} className="branch-connect" data-branch-name={branch} data-branch-source={node.id} draggable={false} onPointerDown={(event) => beginBranchConnection(node.id, branch, event)} title="拖动连接到节点顶部" type="button"></button>
                    </div>
                  )) : null}
                  {node.kind === "jump" ? <span>下一步：{node.target || node.nextStep || "-"}</span> : null}
                </div>
              </article>
            )) : (
              <div className="script-empty-canvas">
                <strong>{currentScene?.name || activeCard.name}</strong>
                <span>{activeSceneId ? "暂无流程节点" : "请先添加场景节点"}</span>
              </div>
            )}
          </div>
        </div>
      );
    }
    if (activeTab === "流程标签") {
      const rows = (processLabelTab === "main" ? flowLabels : ["拒绝", "肯定", "中性", "未识别"]).map((item, index) => [
        index + 1,
        currentScene?.name || "开场",
        processLabelTab === "main" ? "流程节点" : "流程节点",
        item,
        processLabelTab === "main" ? "你好" : item,
        "文字",
        <input type="checkbox" defaultChecked={index === 0} />,
      ]);
      return (
        <section className="script-tab-panel">
          <div className="sub-tabs">
            <button className={processLabelTab === "main" ? "active" : ""} type="button" onClick={() => setProcessLabelTab("main")}>主流程标签</button>
            <button className={processLabelTab === "branch" ? "active" : ""} type="button" onClick={() => setProcessLabelTab("branch")}>分支流程标签</button>
          </div>
          <ScriptFilterBar fields={[["标签名称：", "请输入标签名称"], ["文字内容：", "请输入文字内容"]]} buttons={["查询", "重置"]} />
          <SimpleTable headers={processLabelTab === "main" ? ["序号", "场景节点", "流程节点", "流程标签", "文字内容", "文字类型", "标签精准查询"] : ["序号", "场景节点", "流程节点", "分支类型", "流程标签", "关键字", "标签精准查询"]} rows={rows} />
          {rows.length ? <ScriptFooter total={rows.length} label="全部数据" /> : <DataTips />}
        </section>
      );
    }
    if (activeTab === "知识库") {
      return (
        <section className="script-tab-panel">
          <div className="script-filter-row">
            <label>知识库类型：<select><option>全部</option><option>普通</option><option>用户未说话</option></select></label>
            <label>关键字：<input placeholder="请输入关键字" /></label>
            <button type="button">搜索</button>
            <button type="button" onClick={() => setModal("knowledge")}>添加知识库</button>
            <button type="button" className="plain" onClick={() => openNotice("请选择要删除的知识库。")}>批量删除</button>
          </div>
          <SimpleTable headers={["", "序号", "标题", "问法", "优先排序", "关键词", "知识库标签", "更新时间", "操作"]} rows={knowledge.map((item, index) => [
            <input type="checkbox" />,
            index + 1,
            item.title,
            item.question,
            item.priority,
            item.keywords,
            item.label,
            item.updated,
            <><button type="button" onClick={() => setModal("knowledge")}>编辑</button><button type="button" onClick={() => setKnowledge((rows) => rows.filter((_, rowIndex) => rowIndex !== index))}>删除</button></>,
          ])} />
          <ScriptFooter total={knowledge.length} label="全选 （已选中0条知识库）" />
        </section>
      );
    }
    if (activeTab === "知识库标签") {
      return (
        <section className="script-tab-panel">
          <ScriptFilterBar fields={[["标签名称：", "请输入标签名称"], ["文字内容：", "请输入文字内容"]]} buttons={["查询", "重置"]} />
          <SimpleTable headers={["序号", "知识库标签", "文字内容", "文件类型", "启用查询"]} rows={knowledgeLabels.map((item, index) => [
            index + 1,
            item,
            item === "资费" ? "价格怎么算" : item === "跟进" ? "能不能加微信" : "客户问法",
            "文字",
            <input type="checkbox" defaultChecked={index === 0} />,
          ])} />
          {knowledgeLabels.length ? <ScriptFooter total={knowledgeLabels.length} label="全部数据" /> : <DataTips />}
        </section>
      );
    }
    if (activeTab === "语义标签") {
      return (
        <section className="script-tab-panel">
          <div className="script-filter-row">
            <label><input placeholder="请输入关键字" /></label>
            <button type="button">搜索</button>
            <button type="button" onClick={() => setModal("semantic")}>添加语义标签</button>
            <button type="button" className="plain" onClick={() => openNotice("请选择要删除的语义标签。")}>批量删除</button>
          </div>
          <SimpleTable headers={["", "序号", "标签名称", "关键字", "更新时间", "操作"]} rows={semanticLabels.map((item, index) => [
            <input type="checkbox" />,
            index + 1,
            item,
            item,
            "2025-08-14 12:57:08",
            <><button type="button" onClick={() => setModal("semantic")}>编辑</button><button type="button" onClick={() => setSemanticLabels((rows) => rows.filter((_, rowIndex) => rowIndex !== index))}>删除</button></>,
          ])} />
          <ScriptFooter total={semanticLabels.length} label="全选 （已选中0条语义标签）" />
        </section>
      );
    }
    if (activeTab === "录音管理") {
      return (
        <section className="script-tab-panel">
          <div className="script-filter-row">
            <label>音频来源：<select><option>全部音频</option><option>上传音频</option><option>合成音频</option></select></label>
            <label>文字内容：<input placeholder="请输入文字内容" /></label>
            <button type="button">查询</button>
            <button type="button" onClick={() => setModal("audio")}>合成录音</button>
            <button type="button" onClick={() => setModal("voiceExcel")}>批量上传语音合成文本</button>
            <button type="button" onClick={() => setModal("voiceZip")}>批量上传语音包</button>
          </div>
          <SimpleTable headers={["", "序号", "音频名称", "音频来源", "文字内容", "更新时间", "操作"]} rows={records.map((item, index) => [
            <input type="checkbox" />,
            index + 1,
            item.name,
            item.type,
            item.content,
            "2025-08-14 12:57:08",
            <><button type="button" onClick={() => setModal("audio")}>编辑</button><button type="button" onClick={() => setRecords((rows) => rows.filter((_, rowIndex) => rowIndex !== index))}>删除</button></>,
          ])} />
          <ScriptFooter total={records.length} label="全选 （已选中0条录音）" />
        </section>
      );
    }
    if (activeTab === "等级分类") {
      return (
        <section className="script-tab-panel">
          <div className="script-filter-row">
            <button type="button" onClick={() => setModal("grade")}>添加</button>
            <button type="button" className="plain" onClick={() => openNotice("请选择要删除的意向等级。")}>批量删除</button>
          </div>
          <SimpleTable headers={["", "序号", "等级名称", "等级说明", "操作"]} rows={grades.map((item, index) => [
            <input type="checkbox" />,
            index + 1,
            item.name,
            item.description,
            <><button type="button" onClick={() => setModal("grade")}>编辑</button><button type="button" onClick={() => setGrades((rows) => rows.filter((_, rowIndex) => rowIndex !== index))}>删除</button></>,
          ])} />
          <ScriptFooter total={grades.length} label="全选 （已选中0条意向等级）" />
        </section>
      );
    }
    if (activeTab === "人机训练") {
      return (
        <section className="script-tab-panel">
          <div className="sub-tabs">
            {["全部", "待处理", "已处理", "已忽略"].map((item) => <button className={learningFilter === item ? "active" : ""} key={item} type="button" onClick={() => setLearningFilter(item)}>{item}</button>)}
          </div>
          <SimpleTable headers={["序号", "未识别内容", "处理状态", "记录时间", "操作"]} rows={trainingRows.filter((item) => learningFilter === "全部" || item.status === learningFilter).map((item, index) => [
            index + 1,
            item.text,
            item.status,
            "2025-08-14 12:57:08",
            <><button type="button" onClick={() => setModal("learning")}>处理</button><button type="button" onClick={() => setTrainingRows((rows) => rows.map((row) => row.text === item.text ? { ...row, status: "已忽略" } : row))}>忽略</button></>,
          ])} />
          <DataTips show={trainingRows.filter((item) => learningFilter === "全部" || item.status === learningFilter).length === 0} />
        </section>
      );
    }
    if (activeTab === "变量管理") {
      const allFilteredSelected = filteredVariables.length > 0 && filteredVariables.every((item) => selectedVariableIds.includes(item.id));
      return (
        <section className="script-tab-panel variable-panel">
          <div className="variable-toolbar">
            <div className="variable-filter">
              <label>变量名：</label>
              <input value={variableKeyword} onChange={(event) => setVariableKeyword(event.target.value)} placeholder="请输入关键字" />
              <button type="button">搜索</button>
              <button className="plain" type="button" onClick={() => setVariableKeyword("")}>重置</button>
            </div>
            <div className="variable-actions">
              <button type="button" onClick={() => openNotice("合成音配置已保存到当前变量话术。")}>合成音配置</button>
              <button type="button" onClick={() => openNotice("号码模板下载已生成。")}>号码模板下载</button>
              <button type="button" onClick={() => { setEditingVariableId(null); setModal("variable"); }}>添加变量</button>
              <button className="plain" type="button" onClick={deleteSelectedVariables}>批量删除</button>
            </div>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th><input checked={allFilteredSelected} onChange={(event) => setSelectedVariableIds(event.target.checked ? filteredVariables.map((item) => item.id) : [])} type="checkbox" /></th>
                  <th>序号</th>
                  <th>变量名称</th>
                  <th>变量标识</th>
                  <th>变量示例</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {filteredVariables.map((item, index) => (
                  <tr key={item.id}>
                    <td><input checked={selectedVariableIds.includes(item.id)} onChange={(event) => toggleVariableSelection(item.id, event.target.checked)} type="checkbox" /></td>
                    <td>{index + 1}</td>
                    <td>{item.name}</td>
                    <td>{item.annotation}</td>
                    <td>{item.example}</td>
                    <td>
                      <button type="button" onClick={() => { setEditingVariableId(item.id); setModal("variable"); }}>编辑</button>
                      <button className="plain" type="button" onClick={() => deleteVariable(item.id)}>删除</button>
                    </td>
                  </tr>
                ))}
                {!filteredVariables.length ? <tr><td colSpan={6}><div className="data-tips"><p>暂无相关内容</p></div></td></tr> : null}
              </tbody>
            </table>
          </div>
          <ScriptFooter total={filteredVariables.length} label="全部数据" />
        </section>
      );
    }
    return (
      <section className="script-tab-panel system-config original-config">
        <h3>机器人语言参数配置</h3>
        <label>最小说话时间：<span><input name="min_speak_ms" /> 毫秒</span><em>注：最小说话时间，默认值100，单位毫秒，说话时间小于这个值，会被认为是无效声音</em></label>
        <label>最大说话时间：<span><input name="max_speak_ms" /> 毫秒</span><em>注：最大说话时间，默认值10000，单位毫秒，说话时间超过这个值，就停止录音，直接提交ASR服务器识别</em></label>
        <label>过滤噪音：<span><input name="filter_level" /></span><em>注：防止干扰等级。0-1.0之间，无特殊要求设置为0</em></label>
        <label>机器人音量：<span><input name="volume" /></span><em>注：音量标准化的值。0-100，0不使用音量标准化，其他值 音量把录音音量调整到这个值后，再提交ASR识别</em></label>
        <button type="button" onClick={() => openNotice("系统配置已保存。")}>保存</button>
      </section>
    );
  }

  function renderNodeAudioControls() {
    const previewUrl = absoluteAudioUrl(nodeAudioUrl);
    const promptReadonly = nodeAudioSource === "recording" && Boolean(nodeSelectedAudioId);
    return (
      <div className="node-control-stack node-audio-controls">
        <input name="audioRecordId" type="hidden" value={nodeSelectedAudioId} readOnly />
        <input name="audioRecordSource" type="hidden" value={nodeRecordSource} readOnly />
        <input name="audioUrl" type="hidden" value={nodeAudioUrl} readOnly />
        <input name="audioName" type="hidden" value={nodeAudioName} readOnly />
        <div className="node-radio-line">
          <label>
            <input
              checked={nodeAudioSource === "upload"}
              name="audioSource"
              onChange={() => { setNodeAudioSource("upload"); setNodeAudioMessage(""); }}
              type="radio"
              value="upload"
            /> 本地上传
          </label>
          <label>
            <input
              checked={nodeAudioSource === "recording"}
              name="audioSource"
              onChange={() => { setNodeAudioSource("recording"); loadNodeAudioRecords(nodeRecordSource); }}
              type="radio"
              value="recording"
            /> 录音选择
          </label>
        </div>
        {nodeAudioSource === "upload" ? (
          <input accept=".wav,audio/wav" onChange={uploadNodeAudio} type="file" />
        ) : (
          <div className="node-recording-choice">
            <select
              value={nodeRecordSource}
              onChange={(event) => {
                const nextSource = event.target.value as "all" | "upload" | "synthesis";
                setNodeRecordSource(nextSource);
                setNodeSelectedAudioId("");
                loadNodeAudioRecords(nextSource);
              }}
            >
              <option value="all">全部音频</option>
              <option value="upload">上传音频</option>
              <option value="synthesis">合成音频</option>
            </select>
            <select value={nodeSelectedAudioId} onChange={(event) => chooseNodeAudioRecord(event.target.value)}>
              <option value="">请选择录音</option>
              {audioRecords.map((record) => (
                <option key={record.id} value={record.id}>{record.name}</option>
              ))}
            </select>
          </div>
        )}
        <div className="node-audio-bar">
          {previewUrl ? <audio controls src={previewUrl} /> : <span>{audioBusy ? "处理中..." : "00:00"}</span>}
          {previewUrl ? <button className="node-mini-button" type="button" onClick={() => playNodeAudio()}>试听</button> : null}
        </div>
        {promptReadonly ? <p>录音选择已回填 AI 话术，如需编辑请切换为本地上传。</p> : null}
        {nodeAudioMessage ? <p>{nodeAudioMessage}</p> : null}
      </div>
    );
  }
  function renderModal() {
    if (!modal) return null;
    if (modal === "notice") {
      return <ScriptModal title="操作提示" onClose={() => setModal(null)}><p>{notice}</p><div className="modal-actions"><button type="button" onClick={() => setModal(null)}>确定</button></div></ScriptModal>;
    }
    if (modal === "backup") {
      return <ScriptModal title="操作提示" onClose={() => setModal(null)}><p>确定要备份当前话术？</p><div className="modal-actions"><button type="button" onClick={() => openNotice("话术备份成功。")}>确定</button><button className="plain" type="button" onClick={() => setModal(null)}>取消</button></div></ScriptModal>;
    }
    if (modal === "script") {
      const industryValue = editingCard?.industry === "金融" ? "1" : editingCard?.industry === "贷款" ? "2" : editingCard?.industry === "房产" ? "3" : editingCard?.industry === "装修" ? "4" : editingCard?.industry === "汽车" ? "5" : editingCard?.industry === "教育" ? "6" : editingCard?.industry === "其他" ? "7" : "0";
      return (
        <ScriptModal className="scenario-editor-modal" title={editingCard ? "编辑话术" : "新建话术"} onClose={() => { setEditingScriptIndex(null); setModal(null); }}>
          <form className="scenario-editor-form" onSubmit={saveScript}>
            <div className="scenario-form-row">
              <label>话术名称：</label>
              <input name="name" required defaultValue={editingCard?.name || ""} placeholder="请输入话术名称" />
            </div>
            <div className="scenario-form-row">
              <label>话术类型：</label>
              <select name="is_variable" defaultValue={editingCard?.type === "variable" ? "1" : editingCard?.type === "common" ? "0" : " "}>
                <option value=" ">请选择话术类型</option>
                <option value="1">变量话术</option>
                <option value="0">通用话术</option>
              </select>
            </div>
            <div className="scenario-form-row">
              <label>行业类型：</label>
              <select name="tradeType" defaultValue={industryValue}>
                <option value="0">请选择行业</option>
                <option value="1">金融</option>
                <option value="2">贷款</option>
                <option value="3">房产</option>
                <option value="4">装修</option>
                <option value="5">汽车</option>
                <option value="6">教育</option>
                <option value="7">其他</option>
              </select>
            </div>
            <div className="scenario-form-row">
              <label>自动打断：</label>
              <select name="break" defaultValue={editingCard?.autoBreak === "是" ? "0" : "1"}>
                <option value="0">是</option>
                <option value="1">否</option>
              </select>
            </div>
            <div className="scenario-modal-actions">
              <button type="submit">确 定</button>
              <button className="plain" type="button" onClick={() => { setEditingScriptIndex(null); setModal(null); }}>取消</button>
            </div>
          </form>
        </ScriptModal>
      );
    }
    if (modal === "copy") {
      return <ScriptModal title="复制话术" onClose={() => setModal(null)}><form className="script-modal-form original-modal-form" onSubmit={copyScript}><label>话术名称：<input name="name" required defaultValue={`${activeCard.name}-复制`} /></label><button type="submit">确定</button></form></ScriptModal>;
    }
    if (modal === "import") {
      return <ScriptModal title="导入话术" onClose={() => setModal(null)}><form className="script-modal-form original-modal-form" onSubmit={(event) => { event.preventDefault(); saveScript(event); }}><label>话术名称：<input name="name" required placeholder="导入后话术名称" /></label><input name="is_variable" type="hidden" value="0" readOnly /><input name="tradeType" type="hidden" value="7" readOnly /><input name="break" type="hidden" value="1" readOnly /><label>选择导入文件：<input type="file" /></label><button type="submit">保存</button></form></ScriptModal>;
    }
    if (modal === "dispatch") {
      return <ScriptModal title="话术下发" onClose={() => setModal(null)}><form className="script-modal-form original-modal-form" onSubmit={dispatchScript}><label>目标对象：<select name="target" defaultValue="all"><option value="all">下发到全部机器人</option><option value="selected">下发到指定机器人</option></select></label><label>话术名称：<textarea readOnly defaultValue={activeCard.name} /></label><button type="submit">确定</button></form></ScriptModal>;
    }
    if (modal === "sceneNode") {
      return (
        <ScriptModal className="scenario-editor-modal scene-node-modal" title="添加场景节点" onClose={() => setModal(null)}>
          <form className="scenario-editor-form" onSubmit={addSceneNode}>
            <div className="scenario-form-row">
              <label>场景节点名称：</label>
              <input name="flowname" required placeholder="请输入场景节点名称" />
            </div>
            <div className="scenario-form-row">
              <label>场景节点类型：</label>
              <select name="scenetype" defaultValue="0">
                <option value="0">普通场景节点</option>
                <option value="1">公共场景节点</option>
              </select>
            </div>
            <div className="scenario-modal-actions">
              <button type="submit">确 定</button>
              <button className="plain" type="button" onClick={() => setModal(null)}>取消</button>
            </div>
          </form>
        </ScriptModal>
      );
    }
    if (modal === "flowNode") {
      return (
        <ScriptModal className="node-editor-modal flow-editor-modal" title="流程节点" onClose={() => { setEditingNodeId(null); setModal(null); }}>
          <form className="node-editor-form" onSubmit={(event) => saveCanvasNode("flow", event)}>
            <div className="node-form-row">
              <label className="node-form-label"><span className="node-required">*</span>节点名称：</label>
              <input name="name" required defaultValue={editingNode?.name || "流程节点"} />
            </div>
            <div className="node-form-row">
              <label className="node-form-label">流程标签：</label>
              <input name="label" defaultValue={editingNode?.label || ""} placeholder="请输入流程标签名称" />
            </div>
            <div className="node-form-row">
              <label className="node-form-label">AI话术：</label>
              <div className="node-control-stack">
                <textarea
                  name="prompt"
                  readOnly={nodeAudioSource === "recording" && Boolean(nodeSelectedAudioId)}
                  value={nodePromptDraft}
                  onChange={(event) => setNodePromptDraft(event.target.value)}
                />
                <button className="node-link-button" disabled={audioBusy} onClick={auditionNodePrompt} type="button">在线试听</button>
              </div>
            </div>
            <div className="node-form-row">
              <label className="node-form-label"><span className="node-required">*</span>导入录音：</label>
              {renderNodeAudioControls()}
            </div>
            <div className="node-form-row">
              <label className="node-form-label">用户回答：</label>
              <div className="node-answer-panel">
                <div className="node-branches">
                  {nodeBranchDrafts.map((branch) => (
                    <label className="node-check-line" key={branch}>
                      <input
                        defaultChecked={editingNode ? (editingNode.branches || []).includes(branch) : true}
                        name="branches"
                        type="checkbox"
                        value={branch}
                      /> {branch}
                      <button aria-label={`编辑${branch}`} className="node-pencil" onClick={() => openBranchRuleEditor(branch)} title="编辑识别规则" type="button">✎</button>
                      {!defaultBranches.includes(branch) ? (
                        <button aria-label={`删除${branch}`} className="node-category-remove" onClick={() => removeBranchCategory(branch)} title="删除类别" type="button">×</button>
                      ) : null}
                    </label>
                  ))}
                </div>
                <div className="node-add-category">
                  <input value={newBranchName} onChange={(event) => setNewBranchName(event.target.value)} placeholder="请输入类别名称" />
                  <button type="button" onClick={addBranchCategory}>增加类别</button>
                </div>
                {editingBranchRule ? (
                  <div className="node-rule-editor">
                    <div className="node-rule-title">
                      <strong>{editingBranchRule}</strong>
                      <span>自定义识别规则，多个关键词可用逗号、分号或换行分隔。</span>
                    </div>
                    <textarea value={branchRuleInput} onChange={(event) => setBranchRuleInput(event.target.value)} placeholder="例如：可以、方便、感兴趣" />
                    <div className="node-rule-actions">
                      <button type="button" onClick={saveBranchRule}>保存规则</button>
                      <button className="plain" type="button" onClick={() => { setEditingBranchRule(null); setBranchRuleInput(""); }}>取消</button>
                    </div>
                  </div>
                ) : null}
              </div>
            </div>
            <div className="node-form-row">
              <label className="node-form-label">暂停时间：</label>
              <div className="node-control-stack compact">
                <input name="pauseMs" type="number" defaultValue={editingNode?.pauseMs || 10000} />
                <p>进入该节点之后，延迟一段时间后自动执行，单位毫秒。</p>
              </div>
            </div>
            <div className="node-form-row">
              <label className="node-form-label">其他设置：</label>
              <div className="node-settings-grid">
                <label><input type="checkbox" /> 不允许用户打断</label>
                <label><input type="checkbox" /> 指定未回复</label>
                <label><input type="checkbox" /> 短信通知</label>
                <label><input type="checkbox" /> 人工坐席</label>
                <select><option>选择未回复流程</option></select>
                <select><option>选择短信模板</option></select>
                <select><option>选择坐席组</option></select>
              </div>
            </div>
            <div className="node-modal-actions">
              <button type="submit">确定</button>
              <button className="plain" type="button" onClick={() => { setEditingNodeId(null); setModal(null); }}>取消</button>
            </div>
          </form>
        </ScriptModal>
      );
    }
    if (modal === "jumpNode") {
      return (
        <ScriptModal className="node-editor-modal jump-editor-modal" title="跳转节点" onClose={() => { setEditingNodeId(null); setModal(null); }}>
          <form className="node-editor-form" onSubmit={(event) => saveCanvasNode("jump", event)}>
            <div className="node-form-row">
              <label className="node-form-label"><span className="node-required">*</span>节点名称：</label>
              <input name="name" required defaultValue={editingNode?.name || "跳转节点"} />
            </div>
            <div className="node-form-row">
              <label className="node-form-label">AI话术：</label>
              <div className="node-control-stack">
                <textarea
                  name="prompt"
                  readOnly={nodeAudioSource === "recording" && Boolean(nodeSelectedAudioId)}
                  value={nodePromptDraft}
                  onChange={(event) => setNodePromptDraft(event.target.value)}
                />
                <button className="node-link-button" disabled={audioBusy} onClick={auditionNodePrompt} type="button">在线试听</button>
              </div>
            </div>
            <div className="node-form-row">
              <label className="node-form-label"><span className="node-required">*</span>导入录音：</label>
              {renderNodeAudioControls()}
            </div>
            <div className="node-form-row">
              <label className="node-form-label">下一步：</label>
              <select name="nextStep" defaultValue={editingNode?.nextStep || "指定主动流程"}><option>挂机</option><option>下一主动流程</option><option>指定主动流程</option></select>
            </div>
            <div className="node-form-row">
              <label className="node-form-label">指定的流程节点：</label>
              <select name="target" defaultValue={editingNode?.target || ""}><option value="">选择要跳转到的流程节点</option>{activeSceneNodes.map((node) => <option key={node.id}>{node.name}</option>)}</select>
            </div>
            <div className="node-form-row">
              <label className="node-form-label">暂停时间：</label>
              <div className="node-control-stack compact">
                <input name="pauseMs" type="number" defaultValue={editingNode?.pauseMs || 3000} />
                <p>进入该节点之后，延迟一段时间后自动执行，单位毫秒。</p>
              </div>
            </div>
            <div className="node-modal-actions">
              <button type="submit">确定</button>
              <button className="plain" type="button" onClick={() => { setEditingNodeId(null); setModal(null); }}>取消</button>
            </div>
          </form>
        </ScriptModal>
      );
    }
    if (modal === "variable") {
      return (
        <ScriptModal className="scenario-editor-modal variable-editor-modal" title={editingVariable ? "编辑变量" : "添加变量"} onClose={() => { setEditingVariableId(null); setModal(null); }}>
          <form className="scenario-editor-form" onSubmit={saveVariable}>
            <div className="scenario-form-row variable-form-row">
              <label><span className="node-required">*</span>变量名称：</label>
              <input name="variable_name" required defaultValue={editingVariable?.name || ""} placeholder="如:名字" />
            </div>
            <div className="scenario-form-row variable-form-row">
              <label><span className="node-required">*</span>变量标识：</label>
              <input name="annotation" required defaultValue={editingVariable?.annotation || ""} placeholder="如:{name}" />
            </div>
            <div className="scenario-form-row variable-form-row">
              <label><span className="node-required">*</span>变量示例：</label>
              <input name="example" required defaultValue={editingVariable?.example || ""} placeholder="如:张三" />
            </div>
            <div className="scenario-modal-actions">
              <button className="plain" type="button" onClick={() => { setEditingVariableId(null); setModal(null); }}>取 消</button>
              <button type="submit">确 认</button>
            </div>
          </form>
        </ScriptModal>
      );
    }
    if (modal === "audio") {
      return <ScriptModal title="新建录音" onClose={() => setModal(null)}><form className="script-modal-form" onSubmit={(event) => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); setRecords((items) => [...items, { name: String(data.name), type: "合成音频", content: String(data.content) }]); setModal(null); }}><input name="name" required placeholder="录音名称" /><textarea name="content" required placeholder="文字内容" /><button type="submit">开始合成并保存</button></form></ScriptModal>;
    }
    if (modal === "knowledge") {
      return <ScriptModal title="添加知识库" onClose={() => setModal(null)}><form className="script-modal-form original-modal-form" onSubmit={(event) => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); setKnowledge((items) => [...items, { title: String(data.title || "知识库"), question: String(data.question || ""), priority: Number(data.priority || 10), keywords: String(data.keywords || ""), label: String(data.label || ""), updated: new Date().toLocaleString("zh-CN", { hour12: false }).replace(/\//g, "-") }]); setModal(null); }}><label>标题：<input name="title" required /></label><label>问法：<textarea name="question" /></label><label>优先排序：<input name="priority" type="number" defaultValue={10} /></label><label>关键词：<input name="keywords" /></label><label>知识库标签：<input name="label" /></label><button type="submit">保存</button></form></ScriptModal>;
    }
    if (modal === "semantic") {
      return <ScriptModal title="添加语义标签" onClose={() => setModal(null)}><form className="script-modal-form original-modal-form" onSubmit={(event) => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); setSemanticLabels((items) => [...items, String(data.name || "语义标签")]); setModal(null); }}><label>标签名称：<input name="name" required /></label><label>关键字：<textarea name="keywords" /></label><button type="submit">保存</button></form></ScriptModal>;
    }
    if (modal === "voiceExcel") {
      return <ScriptModal title="选择excel文件" onClose={() => setModal(null)}><form className="script-modal-form original-modal-form"><label>上传文件：<input type="file" /></label><a style={{ cursor: "pointer" }}>下载模板</a><button type="button" onClick={() => openNotice("语音合成文本已保存。")}>保存</button></form></ScriptModal>;
    }
    if (modal === "voiceZip") {
      return <ScriptModal title="选择ZIP文件" onClose={() => setModal(null)}><form className="script-modal-form original-modal-form"><label>上传文件：<input type="file" /></label><a style={{ cursor: "pointer" }}>下载模板</a><button type="button" onClick={() => openNotice("语音包已保存。")}>保存</button></form></ScriptModal>;
    }
    if (modal === "grade") {
      return <ScriptModal title="添加" onClose={() => setModal(null)}><form className="script-modal-form original-modal-form" onSubmit={(event) => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); setGrades((items) => [...items, { name: String(data.name || "等级"), description: String(data.description || "") }]); setModal(null); }}><label>等级名称：<input name="name" required /></label><label>等级说明：<textarea name="description" /></label><button type="submit">确定</button></form></ScriptModal>;
    }
    if (modal === "learning") {
      return <ScriptModal title="处理" onClose={() => setModal(null)}><form className="script-modal-form original-modal-form"><label>未识别内容：<textarea defaultValue="现在不方便" /></label><label>处理方式：<select><option>加入知识库</option><option>忽略</option></select></label><button type="button" onClick={() => openNotice("处理完成。")}>确定</button></form></ScriptModal>;
    }
    if (modal === "simulate") {
      const currentSimNode = simulationNodes().find((node) => node.id === simulateNodeId);
      return (
        <ScriptModal title={`${activeCard.name}--模拟测试`} onClose={() => setModal(null)}>
          <div className="simulate-chat">
            {currentSimNode ? <small>当前节点：{currentSimNode.name}</small> : <small>当前节点：结束/未连接</small>}
            {chat.map((item, index) => <p className={item.role} key={index}>{item.text}</p>)}
          </div>
          <form className="simulate-send" onSubmit={sendChat}>
            <input value={chatInput} onChange={(event) => setChatInput(event.target.value)} placeholder="请输入客户回复" />
            <button type="submit">发送</button>
            <button className="plain" type="button" onClick={startSimulation}>重新开始</button>
          </form>
        </ScriptModal>
      );
    }
    return null;
  }

  return (
    <section className={`legacy-page script-page ${fullscreen ? "script-fullscreen" : ""}`}>
      <aside className="script-left">
        <h2><Icon name="huashuguanli.png" /> 我的话术</h2>
        <div className="script-tabs">
          <button className={scriptType === "common" ? "active" : ""} onClick={() => { setScriptType("common"); const first = scriptCards.findIndex((item) => item.type === "common"); if (first >= 0) selectScript(first); }} type="button">通用话术</button>
          <button className={scriptType === "variable" ? "active" : ""} onClick={() => { setScriptType("variable"); const first = scriptCards.findIndex((item) => item.type === "variable"); if (first >= 0) selectScript(first); }} type="button">变量话术</button>
        </div>
        <p>{visibleScriptCards.length}/100 <span title="话术容量">?</span></p>
        {visibleScriptCards.length ? visibleScriptCards.map(({ scene, index }) => (
          <article className={index === activeScript ? "active" : ""} key={scene.id} onClick={() => selectScript(index)}>
            <strong>{scene.name}</strong>
            <p>行业： {scene.industry}</p>
            <p>审核结果： <a>{scene.audit}</a></p>
            <p>更新时间： {scene.updated}</p>
            <b className={scene.status === "正常" ? "normal" : ""}>{scene.status}</b>
            <div className="script-card-actions">
              <button type="button" onClick={(event) => { event.stopPropagation(); selectScript(index); setEditingScriptIndex(index); setModal("script"); }}>编辑</button>
              <button type="button" onClick={(event) => { event.stopPropagation(); setScriptCards((items) => items.filter((_, rowIndex) => rowIndex !== index)); setActiveScript(0); }}>删除</button>
            </div>
          </article>
        )) : <div className="script-empty-list">暂无话术</div>}
      </aside>
      <section className="script-detail">
        <header>
          <h2><Icon name="renwuxiangqing.png" /> 话术详情</h2>
          <div>
            <button type="button" onClick={() => { setEditingScriptIndex(null); setModal("script"); }}>新建话术</button>
            <button onClick={checkScript} type="button">话术检测</button>
            <button onClick={() => setModal("dispatch")} type="button">话术下发</button>
            <button type="button" onClick={() => { setEditingScriptIndex(null); setModal("import"); }}>导入话术</button>
            <button type="button" onClick={() => setModal("backup")}>话术备份</button>
            <button type="button" onClick={() => setModal("copy")}>复制话术</button>
          </div>
        </header>
        <nav className="script-nav">
          {tabs.map((item) => (
            <button className={activeTab === item ? "active" : ""} key={item} onClick={() => setActiveTab(item)} type="button">{item}</button>
          ))}
        </nav>
        {renderTab()}
        <div className="simulate-drawer">
          <button onClick={startSimulation} type="button">模拟<br />测试</button>
        </div>
      </section>
      {renderModal()}
    </section>
  );
}

function ScriptModal({ title, children, onClose, className = "" }: { title: string; children: any; onClose: () => void; className?: string }) {
  return (
    <div className="script-modal-backdrop">
      <section className={`script-modal ${className}`}>
        <header>
          <h2>{title}</h2>
          <button type="button" onClick={onClose}>×</button>
        </header>
        <div>{children}</div>
      </section>
    </div>
  );
}

function ScriptFilterBar({ fields, buttons }: { fields: Array<[string, string]>; buttons: string[] }) {
  return (
    <div className="script-filter-row">
      {fields.map(([label, placeholder]) => (
        <label key={label}>{label}<input placeholder={placeholder} /></label>
      ))}
      {buttons.map((button) => <button type="button" key={button}>{button}</button>)}
    </div>
  );
}

function ScriptFooter({ total, label }: { total: number; label: string }) {
  return (
    <footer className="script-footer">
      <div>{label}：<span>{total}</span></div>
      <div className="script-paging"></div>
    </footer>
  );
}

function DataTips({ show = true }: { show?: boolean }) {
  if (!show) return null;
  return (
    <div className="data-tips">
      <p>暂无数据</p>
    </div>
  );
}

function ScriptListPanel({ title, items, onAdd, onRemove }: { title: string; items: string[]; onAdd: (value: string) => void; onRemove: (value: string) => void }) {
  return (
    <section className="script-tab-panel">
      <form className="script-inline-form" onSubmit={(event) => {
        event.preventDefault();
        const data = Object.fromEntries(new FormData(event.currentTarget));
        const value = String(data.name || "").trim();
        if (value) onAdd(value);
        event.currentTarget.reset();
      }}>
        <input name="name" required placeholder={`请输入${title}名称`} />
        <button type="submit">添加{title}</button>
      </form>
      <SimpleTable headers={["序号", title, "操作"]} rows={items.map((item, index) => [
        index + 1,
        item,
        <button type="button" onClick={() => onRemove(item)}>删除</button>,
      ])} />
    </section>
  );
}

function CallsReplica({ calls, campaigns, contacts, onCreate, onAction }: { calls: Call[]; campaigns: Campaign[]; contacts: Contact[]; onCreate: (event: FormEvent<HTMLFormElement>) => void; onAction: (id: number, event: "dial" | "answer" | "hangup" | "no_answer" | "busy") => void }) {
  return (
    <section className="legacy-page">
      <div className="page-title"><Icon name="tonghuajilu.png" /> 当前通话记录 <input placeholder="请输入号码查询" /></div>
      <section className="filter-panel">
        <label>任务筛选：<select><option>请选择任务</option>{campaigns.map((item) => <option key={item.id}>{item.name}</option>)}</select></label>
        <label>话术筛选：<select><option>请选择话术</option></select></label>
        <label>拨打时间：<input placeholder="开始拨打时间" /></label>
        <label>至<input placeholder="结束拨打时间" /></label>
        <button>查询</button>
      </section>
      <section className="condition-box">
        <h2>条件筛选 <a>更多条件</a></h2>
        <p><b>意向等级：</b>{["A缓意向", "B级意向", "C级意向", "D级意向", "E级意向", "F级意向"].map((item) => <label key={item}><input type="checkbox" />{item}</label>)}</p>
        <p><b>语义标签：</b></p>
        <p><b>流程标签：</b></p>
        <p><b>问答标签：</b></p>
      </section>
      <div className="record-head">
        <h2>记录列表</h2>
        <div><button>加入CRM</button><button>导出话单</button><button>导出号码</button><button>新建任务拨打</button></div>
      </div>
      <CallTable calls={calls} onAction={onAction} />
      <form className="legacy-form" onSubmit={onCreate}>
        <select name="campaign_id"><option value="">选择任务</option>{campaigns.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select>
        <select name="contact_id"><option value="">选择客户</option>{contacts.map((item) => <option value={item.id} key={item.id}>{item.name || item.phone}</option>)}</select>
        <input name="phone" required placeholder="电话号码" />
        <button type="submit">加入队列</button>
      </form>
    </section>
  );
}

function ContactsReplica({ contacts, onCreate, onImport }: { contacts: Contact[]; onCreate: (event: FormEvent<HTMLFormElement>) => void; onImport: (event: FormEvent<HTMLFormElement>) => void }) {
  return (
    <section className="legacy-page">
      <div className="page-title"><Icon name="crmxitong.png" /> 客户管理</div>
      <section className="condition-box">
        <h2>条件筛选 <input placeholder="请输入号码查询" /></h2>
        <p><b>客户姓名：</b><input placeholder="请输入客户名称" /><b>所属坐席：</b><select><option>全部坐席</option></select><b>所属任务：</b><select><option>全部任务</option></select></p>
        <p><b>意向等级：</b>{["全部", "A级意向", "B级意向", "C级意向"].map((item) => <label key={item}><input type="checkbox" />{item}</label>)}</p>
        <p><b>客户意愿：</b>{["全部", "未分类", "有意向", "沟通中", "试用中", "已成交"].map((item) => <label key={item}><input type="checkbox" />{item}</label>)}</p>
      </section>
      <div className="record-head">
        <h2>客户列表</h2>
        <div><button>查询</button><button>添加客户</button><button>导入</button><button>导出</button><button>分配坐席</button><button>建任务呼叫</button><button>删除</button></div>
      </div>
      <div className="table-wrap">
        <table>
          <thead><tr><th><input type="checkbox" /></th><th>序号</th><th>姓名</th><th>电话</th><th>所属任务</th><th>意向等级</th><th>客户意愿</th><th>创建时间</th><th>最后跟进时间</th><th>操作</th></tr></thead>
          <tbody>
            {contacts.map((contact, index) => <tr key={contact.id}><td><input type="checkbox" /></td><td>{index + 1}</td><td>{contact.name}</td><td>{contact.phone}</td><td>{contact.tags}</td><td>未知</td><td>{contact.notes}</td><td>{contact.created_at || "-"}</td><td>-</td><td>详情</td></tr>)}
          </tbody>
        </table>
      </div>
      <form className="legacy-form" onSubmit={onCreate}>
        <input name="name" placeholder="客户姓名" />
        <input name="phone" required placeholder="手机号码" />
        <input name="tags" placeholder="标签" />
        <input name="notes" placeholder="备注" />
        <button type="submit">保存客户</button>
      </form>
      <section className="manager-form-card compact-card">
        <h2>批量导入号码</h2>
        <form className="legacy-form" onSubmit={onImport}>
          <textarea name="contacts" rows={5} placeholder="每行一个号码，支持：姓名 手机号 标签。例如：张女士 13800000001 A类" />
          <button type="submit">导入号码</button>
          <span>会自动跳过已存在的号码</span>
        </form>
      </section>
    </section>
  );
}

function ManagerReplica({
  title,
  campaigns,
  contacts,
  calls,
}: {
  title: string;
  campaigns: Campaign[];
  contacts: Contact[];
  calls: Call[];
}) {
  if (title === "添加账户") return <AddAccountReplica campaigns={campaigns} />;
  if (title === "账户管理") return <AccountManagementReplica contacts={contacts} campaigns={campaigns} />;
  if (title === "充值管理") return <RechargeReplica contacts={contacts} />;
  if (title === "机器人管理") return <RobotReplica campaigns={campaigns} calls={calls} />;
  if (title === "线路管理") return <LineReplica calls={calls} />;
  if (title === "ASR管理") return <AsrReplica />;
  if (title === "短信通道") return <SmsChannelReplica contacts={contacts} />;
  if (title === "资费管理") return <TariffReplica />;
  if (title === "服务费管理") return <ServiceCostReplica campaigns={campaigns} />;
  return <AccountManagementReplica contacts={contacts} campaigns={campaigns} />;
}

function AddAccountReplica({ campaigns }: { campaigns: Campaign[] }) {
  return (
    <section className="legacy-page manager-page">
      <div className="page-title"><Icon name="pz_touxiang.png" /> 添加账户</div>
      <section className="manager-form-card">
        <h2>账户信息</h2>
        <form className="account-form">
          <LabeledInput required label="用户名" placeholder="请输入用户名" />
          <label><span><b>*</b>用户类型：</span><select defaultValue=""><option value="">请选择账户类型</option><option>运营商账户</option><option>商家账户</option><option>坐席账户</option></select></label>
          <LabeledInput label="手机号码" placeholder="请输入手机号码" />
          <LabeledInput label="备用手机号码" placeholder="请输入备用手机号码" />
          <LabeledInput required label="登录密码" type="password" placeholder="请输入登录密码" />
          <LabeledInput required label="确认密码" type="password" placeholder="请再次输入登录密码" />
          <LabeledInput label="账户充值" placeholder="请输入充值金额" addon="元" />
          <LabeledInput label="机器人个数" placeholder="请输入机器人数量" addon="个" />
          <LabeledInput label="机器人价格" placeholder="请输入机器人单价" addon="元/个" />
          <LabeledInput required label="到期时间" placeholder="请选择到期时间" />
          <LabeledInput label="技术服务费" placeholder="请输入技术服务费" addon="元" />
          <label><span>选择线路：</span><select><option>请选择线路</option><option>LiveKit SIP 默认线路</option><option>3.3测试</option></select></label>
          <label><span>选择ASR：</span><select><option>请选择ASR</option><option>Qwen Realtime ASR</option><option>科大AIUI</option></select></label>
          <label><span>选择短信通道：</span><select><option>请选择短信通道</option><option>默认短信通道</option></select></label>
          <LabeledInput label="可透支额度" placeholder="请输入可透支额度" addon="元" />
          <label><span>是否隐藏话术模块：</span><select><option>否</option><option>是</option></select></label>
          <label><span>是否开启短信验证：</span><select><option>开启</option><option>关闭</option></select></label>
          <label><span>是否开启话术备份：</span><select><option>开启</option><option>关闭</option></select></label>
          <label className="full"><span>备注：</span><textarea rows={6} placeholder="请输入备注信息" defaultValue={campaigns[0]?.prompt || ""} /></label>
          <div className="form-actions"><button type="button">立即提交</button><button className="plain" type="reset">重置</button></div>
        </form>
      </section>
    </section>
  );
}

function AccountManagementReplica({ contacts, campaigns }: { contacts: Contact[]; campaigns: Campaign[] }) {
  const rows = contacts.length ? contacts : [
    { id: 1, name: "测试账户", phone: "13800000001", tags: "运营商", notes: "默认账户" },
    { id: 2, name: "演示客户", phone: "13800000002", tags: "商家账户", notes: "LiveKit测试" },
  ];
  return (
    <section className="legacy-page manager-page">
      <div className="page-title"><Icon name="pz_touxiang.png" /> 账户管理</div>
      <section className="manager-filter">
        <label>账户类型：<select><option>请选择账户类型</option><option>运营商</option><option>商家</option><option>坐席</option></select></label>
        <label>账户状态：<select><option>全部状态</option><option>开启</option><option>锁定</option></select></label>
        <label>用户名：<input placeholder="请输入用户名" /></label>
        <label>手机号：<input placeholder="请输入手机号" /></label>
        <button>查询</button><button className="plain">重置</button>
      </section>
      <ManagerToolbar buttons={["开启", "锁定", "重置密码", "分配机器人", "分配线路", "编辑账户", "删除"]} />
      <SimpleTable
        headers={["序号", "用户名", "用户类型", "手机号码", "余额", "机器人", "任务数", "账户状态", "创建时间", "操作"]}
        rows={rows.map((row, index) => [
          index + 1,
          row.name || `账户${row.id}`,
          row.tags || "商家账户",
          row.phone,
          "0.00",
          campaigns[0]?.max_concurrency || 1,
          campaigns.length,
          "开启",
          row.created_at || "-",
          "编辑 / 充值 / 删除",
        ])}
      />
    </section>
  );
}

function RechargeReplica({ contacts }: { contacts: Contact[] }) {
  return (
    <section className="legacy-page manager-page">
      <div className="page-title"><Icon name="caiwuguanli.png" /> 充值管理</div>
      <section className="manager-filter">
        <label>用户名：<input placeholder="请输入用户名" /></label>
        <label>充值时间：<input placeholder="开始时间" /></label>
        <label>至<input placeholder="结束时间" /></label>
        <label>充值类型：<select><option>全部类型</option><option>账户充值</option><option>机器人充值</option></select></label>
        <button>查询</button><button>账户充值</button>
      </section>
      <section className="manager-form-card compact-card">
        <h2>账户充值</h2>
        <form className="modal-like-form">
          <label>选择账户：<select><option>请选择用户名</option>{contacts.map((contact) => <option key={contact.id}>{contact.name || contact.phone}</option>)}</select></label>
          <label>充值金额：<input placeholder="请输入金额" /></label>
          <label>备注：<textarea rows={4} placeholder="备注充值信息" /></label>
          <button type="button">确认充值</button>
        </form>
      </section>
      <SimpleTable
        headers={["序号", "用户名", "充值金额", "充值前余额", "充值后余额", "充值类型", "充值时间", "备注", "操作"]}
        rows={(contacts.length ? contacts : [{ id: 1, name: "测试", phone: "13800000001", tags: "", notes: "" }]).map((row, index) => [
          index + 1,
          row.name || row.phone,
          "100.00",
          "0.00",
          "100.00",
          "账户充值",
          row.created_at || "-",
          row.notes || "本地演示充值",
          "详情",
        ])}
      />
    </section>
  );
}

function RobotReplica({ campaigns, calls }: { campaigns: Campaign[]; calls: Call[] }) {
  return (
    <section className="legacy-page manager-page">
      <div className="page-title"><Icon name="renwuguanli.png" /> 机器人管理</div>
      <section className="manager-filter">
        <label>用户名：<input placeholder="请输入用户名" /></label>
        <label>用户类型：<select><option>全部类型</option><option>运营商</option><option>商家</option></select></label>
        <label>机器人状态：<select><option>全部状态</option><option>空闲</option><option>通话中</option><option>已回收</option></select></label>
        <button>查询</button><button>分配机器人</button><button className="plain">回收机器人</button>
      </section>
      <div className="manager-summary">
        <article><span>机器人总数</span><strong>{campaigns.reduce((sum, item) => sum + item.max_concurrency, 0) || 1}</strong></article>
        <article><span>通话中</span><strong>{calls.filter((call) => call.status === "active").length}</strong></article>
        <article><span>空闲</span><strong>{Math.max(0, (campaigns[0]?.max_concurrency || 1) - calls.filter((call) => call.status === "active").length)}</strong></article>
        <article><span>已分配任务</span><strong>{campaigns.length}</strong></article>
      </div>
      <SimpleTable
        headers={["序号", "用户名", "用户类型", "机器人总数", "可用机器人", "通话中", "到期时间", "状态", "操作"]}
        rows={(campaigns.length ? campaigns : [{ id: 1, name: "测试任务", max_concurrency: 1, retry_limit: 1, prompt: "", status: "running" } as Campaign]).map((item, index) => [
          index + 1,
          item.name,
          "商家账户",
          item.max_concurrency,
          Math.max(0, item.max_concurrency - calls.filter((call) => call.status === "active").length),
          calls.filter((call) => call.status === "active").length,
          "2026-12-31",
          "开启",
          "编辑机器人 / 回收 / 强制回收",
        ])}
      />
    </section>
  );
}

function LineReplica({ calls }: { calls: Call[] }) {
  return (
    <section className="legacy-page manager-page">
      <div className="page-title"><Icon name="tonghuajilu.png" /> 线路管理</div>
      <div className="manager-tabs"><button>线路列表</button><button>线路分配</button><button>线路统计计费</button></div>
      <section className="manager-filter">
        <label>线路名称：<input placeholder="请输入线路名称" defaultValue="LiveKit SIP 默认线路" /></label>
        <button>查询</button><button>添加线路组</button><button>分配线路</button><button className="plain">线路删除</button>
      </section>
      <SimpleTable
        headers={["序号", "线路组名称", "价格(元/分钟)", "线路数量", "线路组创建时间", "线路来源", "操作", "备注"]}
        rows={[
          [1, "LiveKit SIP 默认线路", "0.08", 1, "2026-06-23", "本地LiveKit", "线路详情 / 编辑 / 删除", "MicroSIP / SIP trunk 测试线路"],
          [2, "3.3测试", "0.10", 1, "2025-03-03", "镜像数据", "线路详情 / 编辑 / 删除", "旧后台复刻"],
        ]}
      />
      <SimpleTable
        title="线路统计计费"
        headers={["序号", "线路名称", "用户名称", "通话时长", "成本价", "成本总额", "销售价", "销售总额", "利润", "计费时间"]}
        rows={(calls.length ? calls : [{ id: 1, phone: "13800000001", status: "completed", room_name: "qwen-call-1", duration_sec: 98, summary: "", intent_level: "high" }]).map((call, index) => [
          index + 1,
          "LiveKit SIP 默认线路",
          call.contact_name || call.phone,
          call.duration_sec,
          "0.05",
          ((call.duration_sec || 0) / 60 * 0.05).toFixed(2),
          "0.08",
          ((call.duration_sec || 0) / 60 * 0.08).toFixed(2),
          ((call.duration_sec || 0) / 60 * 0.03).toFixed(2),
          call.created_at || "-",
        ])}
      />
    </section>
  );
}

function AsrReplica() {
  return (
    <section className="legacy-page manager-page">
      <div className="page-title"><Icon name="huashuguanli.png" /> ASR管理</div>
      <section className="manager-filter">
        <label>ASR名称：<input placeholder="请输入ASR名称" /></label>
        <label>状态：<select><option>全部状态</option><option>启用</option><option>停用</option></select></label>
        <button>查询</button><button>添加ASR</button><button className="plain">批量删除</button>
      </section>
      <SimpleTable
        headers={["序号", "ASR名称", "接口类型", "接口地址", "单价", "状态", "创建时间", "备注", "操作"]}
        rows={[
          [1, "Qwen Realtime ASR", "WebSocket", "DashScope Realtime", "按量", "启用", "2026-06-23", "Agents 当前默认", "编辑 / 停用 / 删除"],
          [2, "科大AIUI", "HTTP", "镜像旧接口", "0.00", "启用", "2025-03-03", "旧后台复刻项", "编辑 / 停用 / 删除"],
        ]}
      />
    </section>
  );
}

function SmsChannelReplica({ contacts }: { contacts: Contact[] }) {
  return (
    <section className="legacy-page manager-page">
      <div className="page-title"><Icon name="duanxinguanli.png" /> 短信通道</div>
      <section className="manager-filter">
        <label>通道名称：<input placeholder="请输入通道名称" /></label>
        <label>状态：<select><option>全部状态</option><option>启用</option><option>停用</option></select></label>
        <button>查询</button><button>添加私有通道</button><button className="plain">删除</button>
      </section>
      <section className="manager-form-card compact-card">
        <h2>添加私有短信通道</h2>
        <form className="modal-like-form two-col">
          <label>通道名称：<input placeholder="请输入通道名称" /></label>
          <label>接口地址：<input placeholder="请输入接口地址" /></label>
          <label>短信ID：<input placeholder="请输入短信ID" /></label>
          <label>短信账号：<input placeholder="请输入短信账号" /></label>
          <label>短信密码：<input type="password" placeholder="请输入短信密码" /></label>
          <label>短信单价：<input placeholder="请输入短信单价" /></label>
          <label>短信数量：<input placeholder="请输入短信数量" /></label>
          <label>备注：<textarea rows={3} placeholder="请输入备注信息" /></label>
          <button type="button">保存通道</button>
        </form>
      </section>
      <SimpleTable
        headers={["序号", "通道名称", "接口地址", "短信账号", "单价", "短信条数", "已分配用户", "状态", "操作"]}
        rows={[
          [1, "默认短信通道", "https://sms.example/api", "demo_sms", "0.05", 1000, contacts.length, "启用", "查看用户 / 分配 / 编辑 / 删除"],
        ]}
      />
    </section>
  );
}

function TariffReplica() {
  return (
    <section className="legacy-page manager-page">
      <div className="page-title"><Icon name="caiwuguanli.png" /> 资费管理</div>
      <section className="manager-filter">
        <label>资费名称：<input placeholder="请输入资费名称" /></label>
        <label>计费类型：<select><option>全部类型</option><option>通话计费</option><option>短信计费</option><option>机器人计费</option></select></label>
        <button>查询</button><button>新增资费</button>
      </section>
      <SimpleTable
        headers={["序号", "资费名称", "计费类型", "成本价", "销售价", "单位", "状态", "备注", "操作"]}
        rows={[
          [1, "LiveKit 通话资费", "通话计费", "0.05", "0.08", "元/分钟", "启用", "SIP通话计费", "编辑 / 停用"],
          [2, "机器人坐席资费", "机器人计费", "0", "100", "元/个/月", "启用", "并发机器人数量", "编辑 / 停用"],
          [3, "短信资费", "短信计费", "0.03", "0.05", "元/条", "启用", "短信模板发送", "编辑 / 停用"],
        ]}
      />
    </section>
  );
}

function ServiceCostReplica({ campaigns }: { campaigns: Campaign[] }) {
  return (
    <section className="legacy-page manager-page">
      <div className="page-title"><Icon name="caiwuguanli.png" /> 服务费管理</div>
      <section className="manager-filter">
        <label>用户名：<input placeholder="请输入用户名" /></label>
        <label>服务类型：<select><option>全部服务</option><option>技术服务费</option><option>话术服务费</option><option>部署服务费</option></select></label>
        <label>时间：<input placeholder="开始时间" /></label>
        <button>查询</button><button>新增服务费</button>
      </section>
      <SimpleTable
        headers={["序号", "用户名称", "服务类型", "服务金额", "关联任务", "计费周期", "状态", "创建时间", "操作"]}
        rows={(campaigns.length ? campaigns : [{ id: 1, name: "测试任务", status: "running", prompt: "", max_concurrency: 1, retry_limit: 1 } as Campaign]).map((item, index) => [
          index + 1,
          "测试账户",
          "技术服务费",
          "100.00",
          item.name,
          "月",
          "启用",
          item.created_at || "-",
          "编辑 / 停用 / 删除",
        ])}
      />
    </section>
  );
}

function LabeledInput({
  label,
  placeholder,
  required = false,
  type = "text",
  addon,
}: {
  label: string;
  placeholder: string;
  required?: boolean;
  type?: string;
  addon?: string;
}) {
  return (
    <label>
      <span>{required ? <b>*</b> : null}{label}：</span>
      <input type={type} placeholder={placeholder} />
      {addon ? <em>{addon}</em> : null}
    </label>
  );
}

function ManagerToolbar({ buttons }: { buttons: string[] }) {
  return <div className="manager-toolbar">{buttons.map((button) => <button key={button}>{button}</button>)}</div>;
}

function SimpleTable({ title, headers, rows }: { title?: string; headers: Array<string | number>; rows: Array<Array<any>> }) {
  return (
    <section className="simple-table-block">
      {title ? <h2>{title}</h2> : null}
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th><input type="checkbox" /></th>{headers.map((header) => <th key={String(header)}>{header}</th>)}</tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={index}>
                <td><input type="checkbox" /></td>
                {row.map((cell, cellIndex) => <td key={`${index}-${cellIndex}`}>{cell}</td>)}
              </tr>
            ))}
            {!rows.length ? <tr><td colSpan={headers.length + 1}><div className="empty-state"><img src="/assets/images/none.png" alt="" /><p>暂无数据</p></div></td></tr> : null}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function UnifiedSubPageReplica({
  title,
  campaigns,
  contacts,
  calls,
  dispatchRecords,
  pushRecords,
  taskTemplates,
  scenes,
  onRetryDispatch,
  onCreatePush,
  onCreateTemplate,
  onCreateCampaignFromTemplate,
}: {
  title: string;
  campaigns: Campaign[];
  contacts: Contact[];
  calls: Call[];
  dispatchRecords: DispatchRecord[];
  pushRecords: PushRecord[];
  taskTemplates: TaskTemplate[];
  scenes: DialogueScene[];
  onRetryDispatch: (id: number) => void;
  onCreatePush: (event: FormEvent<HTMLFormElement>) => void;
  onCreateTemplate: (event: FormEvent<HTMLFormElement>) => void;
  onCreateCampaignFromTemplate: (id: number) => void;
}) {
  if (title === "下发记录") return <DispatchRecordsReplica campaigns={campaigns} calls={calls} records={dispatchRecords} onRetry={onRetryDispatch} />;
  if (title === "推送记录") return <PushRecordsReplica campaigns={campaigns} records={pushRecords} onCreate={onCreatePush} />;
  if (title === "任务模板") return <TaskTemplateReplica campaigns={campaigns} templates={taskTemplates} scenes={scenes} onCreate={onCreateTemplate} onCreateCampaign={onCreateCampaignFromTemplate} />;
  if (title === "坐席管理") return <SeatReplica contacts={contacts} calls={calls} />;
  if (title === "消费明细") return <ConsumptionReplica calls={calls} />;
  if (title === "基础设置") return <SystemSettingReplica />;
  if (["短信签名", "短信模板", "发送记录", "消费记录", "签名审核", "模板审核"].includes(title)) {
    return <SmsReplica title={title} contacts={contacts} campaigns={campaigns} />;
  }
  return <GenericReplica title={title} />;
}

function DispatchRecordsReplica({ campaigns, calls, records, onRetry }: { campaigns: Campaign[]; calls: Call[]; records: DispatchRecord[]; onRetry: (id: number) => void }) {
  const fallbackCall: Call = { id: 1, phone: "13800000001", contact_name: "张女士", campaign_name: "测试任务", status: "pending", room_name: "qwen-call-demo", duration_sec: 0, summary: "", intent_level: "unknown" };
  const fallbackRecords = records.length ? records : (calls.length ? calls : [fallbackCall]).map((call, index) => ({
    id: call.id || index + 1,
    campaign_id: null,
    campaign_name: call.campaign_name || campaigns[0]?.name || "测试任务",
    call_id: call.id,
    phone: call.phone,
    contact_name: call.contact_name || call.caller_name || "-",
    dispatch_type: "LiveKit队列",
    status: call.status === "pending" ? "pending" : "dispatched",
    room_name: call.room_name || "",
    failure_reason: call.failure_reason || "",
    created_at: call.created_at,
  }));
  return (
    <section className="legacy-page">
      <div className="page-title"><Icon name="daochuwenjian.png" /> 下发记录</div>
      <section className="manager-filter">
        <label>任务名称：<select><option>全部任务</option>{campaigns.map((item) => <option key={item.id}>{item.name}</option>)}</select></label>
        <label>下发状态：<select><option>全部状态</option><option>待下发</option><option>已下发</option><option>下发失败</option></select></label>
        <label>创建时间：<input placeholder="开始时间" /></label>
        <label>至<input placeholder="结束时间" /></label>
        <button>查询</button><button>重置</button>
      </section>
      <ManagerToolbar buttons={["批量下发", "导出记录", "刷新列表"]} />
      <SimpleTable
        headers={["序号", "任务名称", "号码", "客户姓名", "下发类型", "下发状态", "房间/线路", "创建时间", "操作"]}
        rows={fallbackRecords.map((record, index) => [
          index + 1,
          record.campaign_name || campaigns.find((item) => item.id === record.campaign_id)?.name || "-",
          record.phone,
          record.contact_name || "-",
          record.dispatch_type || "LiveKit队列",
          statusText[record.status] || (record.status === "dispatched" ? "已下发" : record.status),
          record.room_name || "未创建",
          record.created_at || "-",
          <button type="button" onClick={() => onRetry(record.id)}>重新下发</button>,
        ])}
      />
    </section>
  );
}

function PushRecordsReplica({ campaigns, records, onCreate }: { campaigns: Campaign[]; records: PushRecord[]; onCreate: (event: FormEvent<HTMLFormElement>) => void }) {
  return (
    <section className="legacy-page">
      <div className="page-title"><Icon name="xiaoxizhongxin.png" /> 推送记录</div>
      <section className="manager-filter">
        <label>任务名称：<input placeholder="请输入任务名称" /></label>
        <label>推送类型：<select><option>全部类型</option><option>微信公众号</option><option>企业微信</option><option>Webhook</option></select></label>
        <label>推送状态：<select><option>全部状态</option><option>成功</option><option>失败</option></select></label>
        <button>查询</button><button>新增推送</button>
      </section>
      <section className="manager-form-card compact-card">
        <h2>新增推送</h2>
        <form className="legacy-form" onSubmit={onCreate}>
          <select name="campaign_id"><option value="">选择任务</option>{campaigns.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select>
          <select name="push_type"><option>Webhook</option><option>企业微信</option><option>微信公众号</option></select>
          <input name="target" placeholder="推送目标" defaultValue="Webhook" />
          <input name="content" placeholder="推送内容" />
          <button type="submit">保存推送</button>
        </form>
      </section>
      <SimpleTable
        headers={["序号", "任务名称", "推送目标", "推送内容", "推送状态", "推送时间", "失败原因", "操作"]}
        rows={records.map((item, index) => [
          index + 1,
          item.campaign_name || campaigns.find((campaign) => campaign.id === item.campaign_id)?.name || "-",
          `${item.push_type} / ${item.target}`,
          item.content || "-",
          item.status === "success" ? "成功" : statusText[item.status] || item.status,
          item.created_at || "-",
          item.failure_reason || "-",
          "详情 / 重新推送",
        ])}
      />
    </section>
  );
}

function TaskTemplateReplica({
  campaigns,
  templates,
  scenes,
  onCreate,
  onCreateCampaign,
}: {
  campaigns: Campaign[];
  templates: TaskTemplate[];
  scenes: DialogueScene[];
  onCreate: (event: FormEvent<HTMLFormElement>) => void;
  onCreateCampaign: (id: number) => void;
}) {
  return (
    <section className="legacy-page">
      <div className="page-title"><Icon name="renwuzhongxin.png" /> 任务模板</div>
      <section className="manager-filter">
        <label>模板名称：<input placeholder="请输入模板名称" /></label>
        <label>模板状态：<select><option>全部状态</option><option>启用</option><option>停用</option></select></label>
        <button>查询</button><button>添加模板</button><button className="plain">批量删除</button>
      </section>
      <section className="manager-form-card compact-card">
        <h2>模板配置</h2>
        <form className="modal-like-form two-col" onSubmit={onCreate}>
          <label>模板名称：<input name="name" required placeholder="请输入模板名称" /></label>
          <label>默认并发：<input name="max_concurrency" type="number" placeholder="请输入并发数" defaultValue={2} /></label>
          <label>重试次数：<input name="retry_limit" type="number" placeholder="请输入重试次数" defaultValue={1} /></label>
          <label>默认话术：<select name="default_scene_id"><option value="">通用话术</option>{scenes.map((scene) => <option key={scene.id} value={scene.id}>{scene.name}</option>)}</select></label>
          <label>状态：<select name="status"><option value="enabled">启用</option><option value="disabled">停用</option></select></label>
          <label>备注：<textarea name="notes" rows={3} placeholder="请输入模板说明" /></label>
          <label className="full">默认提示词：<textarea name="default_prompt" rows={4} placeholder="请输入默认任务提示词" /></label>
          <button type="submit">保存模板</button>
        </form>
      </section>
      <SimpleTable
        headers={["序号", "模板名称", "默认话术", "并发数", "重试次数", "状态", "创建时间", "操作"]}
        rows={templates.map((item, index) => [
          index + 1,
          item.name,
          item.scene_name || "通用话术",
          item.max_concurrency,
          item.retry_limit,
          item.status === "enabled" ? "启用" : "停用",
          item.created_at || "-",
          <button type="button" onClick={() => onCreateCampaign(item.id)}>生成任务</button>,
        ])}
      />
    </section>
  );
}

function SeatReplica({ contacts, calls }: { contacts: Contact[]; calls: Call[] }) {
  const fallback: Contact = { id: 1, name: "默认坐席", phone: "13800000001", tags: "默认分组", notes: "" };
  return (
    <section className="legacy-page">
      <div className="page-title"><Icon name="crmxitong.png" /> 坐席管理</div>
      <section className="manager-filter">
        <label>坐席姓名：<input placeholder="请输入坐席姓名" /></label>
        <label>坐席状态：<select><option>全部状态</option><option>在线</option><option>离线</option><option>锁定</option></select></label>
        <label>所属分组：<select><option>全部分组</option><option>默认分组</option></select></label>
        <button>查询</button><button>添加坐席</button><button>添加分组</button>
      </section>
      <div className="manager-summary">
        <article><span>坐席总数</span><strong>{Math.max(contacts.length, 1)}</strong></article>
        <article><span>在线坐席</span><strong>{calls.filter((call) => call.status === "active").length}</strong></article>
        <article><span>今日跟进</span><strong>{calls.length}</strong></article>
        <article><span>锁定坐席</span><strong>0</strong></article>
      </div>
      <SimpleTable
        headers={["序号", "坐席姓名", "手机号", "所属分组", "当前状态", "分配客户", "今日通话", "创建时间", "操作"]}
        rows={(contacts.length ? contacts : [fallback]).map((item, index) => [
          index + 1,
          item.name || `坐席${index + 1}`,
          item.phone,
          item.tags || "默认分组",
          calls[index]?.status === "active" ? "在线" : "离线",
          1,
          calls.filter((call) => call.contact_name === item.name).length,
          item.created_at || "-",
          "编辑 / 锁定 / 删除",
        ])}
      />
    </section>
  );
}

function SmsReplica({ title, contacts, campaigns }: { title: string; contacts: Contact[]; campaigns: Campaign[] }) {
  const isTemplate = title.includes("模板");
  const isAudit = title.includes("审核");
  const isRecord = title.includes("记录");
  const fallbackCampaign: Campaign = { id: 1, name: "默认短信模板", prompt: "您好，感谢您的接听。", status: "draft", max_concurrency: 1, retry_limit: 1 };
  return (
    <section className="legacy-page">
      <div className="page-title"><Icon name="duanxinguanli.png" /> {title}</div>
      <section className="manager-filter">
        <label>{isTemplate ? "模板名称" : title.includes("签名") ? "签名名称" : "手机号"}：<input placeholder={isTemplate ? "请输入模板名称" : "请输入查询关键字"} /></label>
        <label>创建时间：<input placeholder="选择开始日期" /></label>
        <label>至<input placeholder="选择结束日期" /></label>
        <label>审核状态：<select><option>请选择审核状态</option><option>待审核</option><option>审核通过</option><option>审核失败</option></select></label>
        <button>查询</button><button>重置</button><button>{isTemplate ? "添加短信模板" : title.includes("签名") ? "添加短信签名" : "导出"}</button>
      </section>
      {isTemplate || title.includes("签名") ? (
        <section className="manager-form-card compact-card">
          <h2>{isTemplate ? "模板内容" : "签名内容"}</h2>
          <form className="modal-like-form two-col">
            <label>{isTemplate ? "模板名称" : "签名名称"}：<input placeholder="请输入名称" /></label>
            <label>关联通道：<select><option>默认短信通道</option></select></label>
            <label>内容：<textarea rows={4} placeholder={isTemplate ? "请输入短信模板内容" : "请输入签名内容"} /></label>
            <button type="button">保存</button>
          </form>
        </section>
      ) : null}
      <SimpleTable
        headers={isRecord ? ["序号", "接收号码", "短信内容", "关联任务", "发送状态", "发送时间", "计费条数", "操作"] : ["序号", isTemplate ? "模板名称" : "签名名称", "通道名称", "内容", "审核状态", "创建时间", "审核人", "操作"]}
        rows={(isRecord ? contacts : campaigns.length ? campaigns : [fallbackCampaign]).map((item: Contact | Campaign, index) => {
          if (isRecord) {
            const contact = item as Contact;
            return [index + 1, contact.phone, "您好，感谢您的接听。", campaigns[0]?.name || "测试任务", "成功", contact.created_at || "-", 1, "详情"];
          }
          const campaign = item as Campaign;
          return [index + 1, campaign.name, "默认短信通道", campaign.prompt || "短信内容示例", isAudit ? "待审核" : "审核通过", campaign.created_at || "-", "系统", "编辑 / 提交审核 / 删除"];
        })}
      />
    </section>
  );
}

function ConsumptionReplica({ calls }: { calls: Call[] }) {
  const fallback: Call = { id: 1, phone: "13800000001", status: "completed", room_name: "qwen-call-demo", duration_sec: 120, summary: "", intent_level: "high" };
  const rows = (calls.length ? calls : [fallback]).map((call, index) => {
    const minutes = Math.max(1, Math.ceil((call.duration_sec || 0) / 60));
    return [index + 1, call.contact_name || call.phone, "通话消费", `${minutes}分钟`, "0.08元/分钟", (minutes * 0.08).toFixed(2), call.created_at || "-", "LiveKit SIP"];
  });
  return (
    <section className="legacy-page">
      <div className="page-title"><Icon name="caiwuguanli.png" /> 消费明细</div>
      <section className="manager-filter">
        <label>账户名称：<input placeholder="请输入账户名称" /></label>
        <label>消费类型：<select><option>全部类型</option><option>通话消费</option><option>短信消费</option><option>机器人消费</option></select></label>
        <label>消费时间：<input placeholder="开始时间" /></label>
        <label>至<input placeholder="结束时间" /></label>
        <button>查询</button><button>导出</button>
      </section>
      <div className="manager-summary">
        <article><span>总消费</span><strong>{rows.reduce((sum, row) => sum + Number(row[5]), 0).toFixed(2)}</strong></article>
        <article><span>通话条数</span><strong>{calls.length}</strong></article>
        <article><span>短信条数</span><strong>0</strong></article>
        <article><span>机器人消费</span><strong>0.00</strong></article>
      </div>
      <SimpleTable headers={["序号", "账户名称", "消费类型", "消费数量", "单价", "消费金额", "消费时间", "备注"]} rows={rows} />
    </section>
  );
}

function SystemSettingReplica() {
  return (
    <section className="legacy-page">
      <div className="page-title"><Icon name="pz_xitongguanli.png" /> 基础设置</div>
      <section className="manager-form-card">
        <h2>系统基础配置</h2>
        <form className="account-form">
          <LabeledInput label="系统名称" placeholder="AI智能机器人" />
          <LabeledInput label="LiveKit地址" placeholder="ws://127.0.0.1:7880" />
          <LabeledInput label="Agents API" placeholder="http://127.0.0.1:8091" />
          <LabeledInput label="SIP端口" placeholder="5066" />
          <label><span>默认ASR：</span><select><option>Qwen Realtime ASR</option><option>Qwen HTTP ASR</option></select></label>
          <label><span>默认TTS：</span><select><option>Qwen TTS</option></select></label>
          <label><span>话术优先：</span><select><option>开启</option><option>关闭</option></select></label>
          <label><span>通话录音：</span><select><option>开启</option><option>关闭</option></select></label>
          <label className="full"><span>运营说明：</span><textarea rows={6} placeholder="请输入系统公告或运营说明" /></label>
          <div className="form-actions"><button type="button">保存设置</button><button className="plain" type="reset">重置</button></div>
        </form>
      </section>
    </section>
  );
}

function GenericReplica({ title }: { title: string }) {
  return (
    <section className="legacy-page">
      <div className="page-title"><Icon name="pz_xitongguanli.png" /> {title}</div>
      <section className="manager-filter">
        <label>关键词：<input placeholder="请输入查询关键字" /></label>
        <button>查询</button><button>新增</button><button className="plain">删除</button>
      </section>
      <SimpleTable headers={["序号", "名称", "类型", "状态", "创建时间", "操作"]} rows={[[1, title, "本地复刻", "启用", "-", "编辑 / 删除"]]} />
    </section>
  );
}

function CallTable({ calls, compact = false, onAction }: { calls: Call[]; compact?: boolean; onAction?: (id: number, event: "dial" | "answer" | "hangup" | "no_answer" | "busy") => void }) {
  function intentText(call: Call) {
    if (call.dialogue_label) return call.dialogue_label;
    if (call.intent_level === "high") return "A级意向";
    if (call.intent_level === "medium") return "B级意向";
    if (call.intent_level === "low") return "C级意向";
    return "未知";
  }

  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th><input type="checkbox" /></th><th>序号</th><th>姓名</th><th>手机号码</th><th>通话时长(S)</th><th>状态</th><th>意向等级</th><th>任务名称</th><th>话术名称</th><th>拨打时间</th>{compact ? null : <th>操作</th>}</tr></thead>
        <tbody>
          {calls.map((call, index) => (
            <tr key={call.id}>
              <td><input type="checkbox" /></td>
              <td>{index + 1}</td>
              <td>{call.contact_name || call.caller_name || "-"}</td>
              <td>{call.phone}</td>
              <td>{call.live_duration_sec ?? call.duration_sec ?? 0}</td>
              <td>{statusText[call.status] || call.status}</td>
              <td>{intentText(call)}</td>
              <td>{call.campaign_name || "-"}</td>
              <td>{call.scene_name || "通用"}</td>
              <td>{call.created_at || "-"}</td>
              {compact ? null : (
                <td>
                  <button onClick={() => onAction?.(call.id, "dial")} type="button">拨号</button>
                  <button onClick={() => onAction?.(call.id, "answer")} type="button">接听</button>
                  <button onClick={() => onAction?.(call.id, "hangup")} type="button">挂断</button>
                  <button onClick={() => onAction?.(call.id, "no_answer")} type="button">无人接听</button>
                  <button onClick={() => onAction?.(call.id, "busy")} type="button">忙线</button>
                </td>
              )}
            </tr>
          ))}
          {!calls.length ? <tr><td colSpan={compact ? 10 : 11}><div className="empty-state"><img src="/assets/images/none.png" alt="" /><p>暂无数据</p></div></td></tr> : null}
        </tbody>
      </table>
    </div>
  );
}

function ProgressColumns() {
  const groups = [
    ["客户意向等级", "A级(意向客户)", "B级(一般意向)", "C级(简单对话)", "D级(无有效对话)", "E级(有效未接通)", "F级(无效号码)"],
    ["通话时长", "1-9s", "10-17s", "18-39s", ">40s以上"],
    ["说话次数", "1-2次", "3-4次", "5-6次", "7-10次", "10次以上"],
  ];
  return <div className="progress-cols">{groups.map((group) => <section key={group[0]}><h3>{group[0]}</h3>{group.slice(1).map((item) => <p key={item}><span>{item}</span><i></i><b>0%</b></p>)}</section>)}</div>;
}

function TableLite({ rows }: { rows: Array<[string, string | number]> }) {
  return <dl className="table-lite">{rows.map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl>;
}

function Icon({ name }: { name: string }) {
  return <img className="title-icon" src={`/assets/images/${name}`} alt="" />;
}

function parentTitle(activeMenu: string) {
  return menuGroups.find((group) => group.items.some((item) => item.label === activeMenu))?.title.replace(/\s+/g, "") || "控制台";
}

function labelFromSource(source: string) {
  const map: Record<string, string> = {
    "/user/index/index": "综合概况",
    "/user/plan/newindex": "任务管理",
    "/user/plan/newadd": "添加任务",
    "/user/plan/task_statistics": "任务统计",
    "/user/plan/phone_manage": "号码管理",
    "/user/plan/sendout_phone": "下发记录",
    "/user/scenarios/scene": "话术配置",
    "/user/scenarios/sendout_scene": "话术下发记录",
    "/user/callrecord/current_record": "当天通话记录",
    "/user/callrecord/historical_records": "历史通话记录",
    "/user/member/intentional_member": "客户管理",
    "/user/sms/signature": "短信签名",
    "/user/sms/template": "短信模板",
    "/user/sms/sendrecord": "发送记录",
    "/user/system/setting": "系统设置",
    "/user/manager/account_management": "账户管理",
  };
  return map[source] || source.split("/").filter(Boolean).slice(-1)[0].replace(/_/g, " ");
}

function App() {
  const path = window.location.pathname;
  if (path === "/terms" || path === "/privacy") return <Suspense fallback={<div className="route-loading">正在打开文档…</div>}><LegalDocument kind={path === "/terms" ? "terms" : "privacy"} /></Suspense>;
  if (path.startsWith("/experience/voice")) return <Suspense fallback={<div className="route-loading">正在准备语音体验…</div>}><InboundExperience /></Suspense>;
  if (path.startsWith("/app/home")) return <Suspense fallback={<div className="route-loading">正在打开工作空间…</div>}><WorkspaceHome /></Suspense>;
  if (path.startsWith("/app/inbound/knowledge")) return <Suspense fallback={<div className="route-loading">正在打开知识库…</div>}><KnowledgeConsole /></Suspense>;
  if (path.startsWith("/app/inbound/integrations")) return <Suspense fallback={<div className="route-loading">正在打开业务系统…</div>}><IntegrationConsole /></Suspense>;
  if (path.startsWith("/app/inbound/content")) return <Suspense fallback={<div className="route-loading">正在打开展示素材…</div>}><ContentConsole /></Suspense>;
  if (path.startsWith("/app/inbound/evaluation")) return <Suspense fallback={<div className="route-loading">正在打开评测…</div>}><EvaluationConsole /></Suspense>;
  if (path.startsWith("/app/inbound")) return <Suspense fallback={<div className="route-loading">正在打开智能呼入…</div>}><InboundConsole /></Suspense>;
  return <LegacyApp />;
}

export default App;
