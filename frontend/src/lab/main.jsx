import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import LabApp from "./LabApp.jsx";
import "../styles.css";
import "./lab.css";

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <LabApp />
  </StrictMode>,
);
