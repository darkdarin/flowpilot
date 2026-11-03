import { ref } from "vue";

const en = {
  "app.title": "FlowPilot",
  "login.title": "Connect to the runtime",
  "login.hint": "Paste the access token from the runtime configuration (token or token_file).",
  "login.token": "Access token",
  "login.connect": "Connect",
  "login.bad": "The runtime rejected this token.",
  "graph.pick": "Graph",
  "graph.new": "New graph",
  "graph.newId": "Graph id",
  "graph.newName": "Name",
  "graph.create": "Create graph",
  "graph.cancel": "Cancel",
  "state.live": "Deployed",
  "state.draft": "Draft",
  "state.notDeployed": "Not deployed",
  "state.saving": "Saving…",
  "state.saved": "Draft saved",
  "action.deploy": "Deploy",
  "action.deploying": "Deploying…",
  "action.deployed": "Deployed",
  "action.discard": "Discard draft",
  "action.discardConfirm": "Discard the draft and go back to the deployed graph?",
  "action.fit": "Fit to view",
  "action.debug": "Live debug",
  "theme.light": "Light theme",
  "theme.dark": "Dark theme",
  "palette.search": "Find a node",
  "palette.hint": "Drag a node onto the canvas",
  "palette.empty": "No nodes match.",
  "category.input": "Input",
  "category.output": "Output",
  "category.speech": "Speech",
  "category.agent": "Agents",
  "category.logic": "Logic",
  "category.identity": "Identity",
  "category.other": "Other",
  "inspector.empty": "Select a node to edit its settings.",
  "inspector.id": "Node id",
  "inspector.idTaken": "Another node already uses this id.",
  "inspector.settings": "Settings",
  "inspector.noSettings": "This node has no settings.",
  "inspector.ports": "Ports",
  "inspector.inputs": "Inputs",
  "inspector.outputs": "Outputs",
  "inspector.needsHost": "Needs Home Assistant",
  "inspector.delete": "Delete node",
  "inspector.required": "required",
  "inspector.optional": "optional",
  "inspector.unknownType": "This node type is not installed in the runtime.",
  "form.secret": "Secret name",
  "form.secretHint": "Stored in the runtime secrets file, never in the graph.",
  "form.unset": "Not set",
  "form.invalidJson": "Not valid JSON",
  "issues.none": "No problems",
  "issues.count": "{n} problems",
  "issues.one": "1 problem",
  "issues.title": "Problems",
  "debug.title": "Live debug",
  "debug.waiting": "Waiting for a turn. Speak to a satellite or send text from the dev client.",
  "debug.offline": "Trace stream disconnected, reconnecting…",
  "debug.turn": "Turn",
  "debug.items": "{n} items",
  "debug.redeployed": "A new version of this graph was deployed.",
  "status.ok": "ok",
  "status.degraded": "degraded",
  "status.timeout": "timeout",
  "status.error": "error",
  "status.cancelled": "cancelled",
  "status.running": "running",
  "error.load": "Could not load from the runtime: {msg}",
  "error.deploy": "Deploy failed: {msg}",
  "error.invalid": "Fix the problems before deploying.",
  "error.connect": "These ports carry different data types.",
};

type Key = keyof typeof en;

const ru: Record<Key, string> = {
  "app.title": "FlowPilot",
  "login.title": "Подключение к рантайму",
  "login.hint": "Вставьте токен доступа из конфигурации рантайма (token или token_file).",
  "login.token": "Токен доступа",
  "login.connect": "Подключиться",
  "login.bad": "Рантайм не принял этот токен.",
  "graph.pick": "Граф",
  "graph.new": "Новый граф",
  "graph.newId": "Идентификатор",
  "graph.newName": "Название",
  "graph.create": "Создать граф",
  "graph.cancel": "Отмена",
  "state.live": "Развёрнут",
  "state.draft": "Черновик",
  "state.notDeployed": "Не развёрнут",
  "state.saving": "Сохраняю…",
  "state.saved": "Черновик сохранён",
  "action.deploy": "Развернуть",
  "action.deploying": "Разворачиваю…",
  "action.deployed": "Развёрнуто",
  "action.discard": "Отменить черновик",
  "action.discardConfirm": "Удалить черновик и вернуться к развёрнутой версии?",
  "action.fit": "Показать весь граф",
  "action.debug": "Живая отладка",
  "theme.light": "Светлая тема",
  "theme.dark": "Тёмная тема",
  "palette.search": "Найти ноду",
  "palette.hint": "Перетащите ноду на холст",
  "palette.empty": "Ничего не найдено.",
  "category.input": "Вход",
  "category.output": "Выход",
  "category.speech": "Речь",
  "category.agent": "Агенты",
  "category.logic": "Логика",
  "category.identity": "Личность",
  "category.other": "Прочее",
  "inspector.empty": "Выберите ноду, чтобы настроить её.",
  "inspector.id": "Id ноды",
  "inspector.idTaken": "Этот id уже занят другой нодой.",
  "inspector.settings": "Настройки",
  "inspector.noSettings": "У этой ноды нет настроек.",
  "inspector.ports": "Порты",
  "inspector.inputs": "Входы",
  "inspector.outputs": "Выходы",
  "inspector.needsHost": "Нужен Home Assistant",
  "inspector.delete": "Удалить ноду",
  "inspector.required": "обязательный",
  "inspector.optional": "необязательный",
  "inspector.unknownType": "Этот тип ноды не установлен в рантайме.",
  "form.secret": "Имя секрета",
  "form.secretHint": "Хранится в файле секретов рантайма, не в графе.",
  "form.unset": "Не задано",
  "form.invalidJson": "Некорректный JSON",
  "issues.none": "Проблем нет",
  "issues.count": "Проблем: {n}",
  "issues.one": "1 проблема",
  "issues.title": "Проблемы",
  "debug.title": "Живая отладка",
  "debug.waiting": "Жду ход. Скажите что-нибудь сателлиту или отправьте текст из dev-клиента.",
  "debug.offline": "Поток трассировки отключён, переподключаюсь…",
  "debug.turn": "Ход",
  "debug.items": "элементов: {n}",
  "debug.redeployed": "Развёрнута новая версия этого графа.",
  "status.ok": "ok",
  "status.degraded": "частично",
  "status.timeout": "таймаут",
  "status.error": "ошибка",
  "status.cancelled": "отменено",
  "status.running": "выполняется",
  "error.load": "Не удалось загрузить данные из рантайма: {msg}",
  "error.deploy": "Не удалось развернуть: {msg}",
  "error.invalid": "Исправьте проблемы перед развёртыванием.",
  "error.connect": "Эти порты передают разные типы данных.",
};

export type Locale = "en" | "ru";
const dictionaries: Record<Locale, Record<Key, string>> = { en, ru };

function initialLocale(): Locale {
  const saved = localStorage.getItem("flowpilot.locale");
  if (saved === "en" || saved === "ru") return saved;
  return navigator.language.toLowerCase().startsWith("ru") ? "ru" : "en";
}

export const locale = ref<Locale>(initialLocale());

export function setLocale(value: Locale) {
  locale.value = value;
  localStorage.setItem("flowpilot.locale", value);
  document.documentElement.lang = value;
}

export function t(key: Key, params: Record<string, string | number> = {}): string {
  const text = dictionaries[locale.value][key] ?? key;
  return text.replace(/\{(\w+)\}/g, (_, name: string) => String(params[name] ?? ""));
}

export function categoryLabel(category: string): string {
  const key = `category.${category}` as Key;
  return key in en ? t(key) : category;
}
