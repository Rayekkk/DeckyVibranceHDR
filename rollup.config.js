import { readFileSync, rmSync } from "node:fs";
import typescript from "@rollup/plugin-typescript";
import { nodeResolve } from "@rollup/plugin-node-resolve";
import externalGlobals from "rollup-plugin-external-globals";

const manifest = JSON.parse(readFileSync(new URL("./plugin.json", import.meta.url), "utf8"));
// Keep the Decky host globals and manifest contract without its unused asset
// and deletion plugins (which pulled in vulnerable glob dependencies).
export default {
  input: "src/index.tsx", context: "window",
  external: ["react", "react-dom", "@decky/ui"],
  plugins: [{ name: "clean-plugin-output", buildStart() {
      rmSync(new URL("./dist", import.meta.url), { recursive: true, force: true });
    } }, typescript(), nodeResolve({ browser: true }),
    externalGlobals({ react: "SP_REACT", "react/jsx-runtime": "SP_JSX",
      "react-dom": "SP_REACTDOM", "@decky/ui": "DFL",
      "@decky/manifest": JSON.stringify(manifest) })],
  output: { dir: "dist", format: "esm", exports: "default", sourcemap: true,
    sourcemapPathTransform: path => path.replace(/^\.\.\//,
      `decky://decky/plugin/${encodeURIComponent(manifest.name)}/`) },
};
