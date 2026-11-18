import type { Config } from "tailwindcss";

export default {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
      },
      colors: {
        surface: "#fcfcfb",
        ink: { DEFAULT: "#0b0b0b", 2: "#52514e", 3: "#8a8984" },
        rule: "#e7e6e2",
        // categorical slots 1-3 (validated all-pairs, see docs/SPEC.md §UI)
        cow: "#2a78d6",
        zombie: "#eb6834",
        anchor: "#1baf7a",
        other: "#b4b3ad",
      },
    },
  },
  plugins: [],
} satisfies Config;
