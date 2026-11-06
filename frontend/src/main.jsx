import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import AssistantLab from "./AssistantLab.jsx";

const View = new URLSearchParams(location.search).has("assistant") ? AssistantLab : App;
document.body.classList.toggle("assistant-view", View === AssistantLab);
createRoot(document.getElementById("root")).render(<View />);
