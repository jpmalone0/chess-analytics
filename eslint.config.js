import js from "@eslint/js";

export default [
    js.configs.recommended,
    {
        files: ["app/static/**/*.js"],
        languageOptions: {
            ecmaVersion: 2022,
            globals: {
                document: "readonly",
                window: "readonly",
                fetch: "readonly",
                console: "readonly",
                Chart: "readonly",
                localStorage: "readonly",
                setTimeout: "readonly",
                Promise: "readonly",
            },
        },
        rules: {
            "no-unused-vars": ["warn", {
                "varsIgnorePattern": "^(loadPlayer|nextPage|prevPage|openGameDetail|closeModal|resetDateRange|escapeHtml|toggleCompare|loadComparePlayer|exitCompareMode|nextPageCompare|prevPageCompare|toggleProjection|toggleFitMode)$"
            }],
            "no-undef": "error",
            "no-console": "off",
        },
    },
];
