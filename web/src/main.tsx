import React from "react";
import ReactDOM from "react-dom/client";
import "@fontsource-variable/source-serif-4";
import "@fontsource-variable/inter";
import "@fontsource-variable/jetbrains-mono";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/shell.css";
import "./styles/ledger.css";
import "./styles/detail.css";
import "./styles/forms.css";
import "./styles/articles.css";
import App from "./App";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
