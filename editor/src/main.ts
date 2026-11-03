import "@fontsource-variable/onest";
import "@fontsource-variable/jetbrains-mono";
import "./style.css";
import { createApp } from "vue";
import App from "./App.vue";
import { locale } from "./i18n";

document.documentElement.lang = locale.value;
const theme = localStorage.getItem("flowpilot.theme")
  ?? (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
document.documentElement.dataset.theme = theme;

createApp(App).mount("#app");
