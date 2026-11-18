# FeatureLedger: מדריך הפעלה צעד אחר צעד

המדריך בנוי משני חלקים:

1. **הרצת הדמו** (כ-15 דקות): המערכת עולה עם חברה לדוגמה, Northwind Cloud, כדי שתראה איך הכול עובד.
2. **מעבר לנתונים של החברה שלך**: מגדירים את הפיצ'רים, מחברים את GitHub ומייבאים לקוחות, שימוש, טיקטים ועלויות מקובצי CSV.

> בכל מקום שכתוב "בטרמינל", הכוונה היא ל-**PowerShell** ב-Windows או ל-**Terminal** ב-Mac.
> כשהפקודה שונה בין המערכות, מופיעות שתי הגרסאות.

---

## חלק א׳: מה צריך להתקין (פעם אחת)

### אפשרות 1: על המחשב שלך (מומלץ)

1. **Docker Desktop**: https://www.docker.com/products/docker-desktop/
   - מורידים ומתקינים. ב-Windows, אם ההתקנה מבקשת להפעיל WSL 2, מאשרים.
   - פותחים את Docker Desktop ומחכים שבפינה יופיע **Engine running**. בלי זה שום דבר לא יעבוד.
2. **Git**: https://git-scm.com/downloads (ב-Mac לרוב כבר מותקן).
3. בודקים בטרמינל:
   ```
   docker --version
   docker compose version
   git --version
   ```
   כל אחת מהפקודות צריכה להדפיס מספר גרסה.

### אפשרות 2: בלי להתקין כלום, בלחיצה אחת (GitHub Codespaces), הכי פשוט

1. נכנסים לדף הריפו ב-GitHub ולוחצים על הכפתור **Open in GitHub Codespaces** שבראש הדף.
   - דרך נוספת: הכפתור הירוק **Code**, אחר כך הלשונית **Codespaces**, ואז **Create codespace on main**.
2. נפתח VS Code בדפדפן. **לא צריך להקליד כלום**: המערכת נבנית ועולה לבד, וזה לוקח 3–6 דקות בפעם הראשונה.
   - אפשר לעקוב בטרמינל שבתחתית המסך. כשהכול מוכן מופיעה השורה `✅ FeatureLedger is up`.
3. הדשבורד נפתח לבד בלשונית חדשה. אם הדפדפן חסם את החלון: לשונית **PORTS** בתחתית, ואז סמל הגלובוס ליד פורט **3000**.
4. בסיום עבודה: בתפריט ☰ בוחרים **Codespaces: Stop Current Codespace**, כדי לא לבזבז שעות חינמיות.
   - בחשבון חינמי יש 120 שעות ליבה בחודש, כלומר כ-60 שעות שימוש במכונה הבסיסית.
   - בפעם הבאה: **Code**, אחר כך **Codespaces**, ולוחצים על ה-Codespace הקיים. הוא עולה תוך דקה.

---

## חלק ב׳: הרצת הדמו

> ב-Codespaces צעדים 1–3 קורים לבד. מדלגים ישר לצעד 4. הכתובות הן בלשונית PORTS, ולא localhost.

### צעד 1: הורדת הקוד

בטרמינל:
```
git clone https://github.com/yakov-ashgar27/FeatureLedger-Feature-P-L-Safe-Deprecation-Radar.git
cd FeatureLedger-Feature-P-L-Safe-Deprecation-Radar
```

### צעד 2: יצירת קובץ ההגדרות `.env`

- Mac או Codespaces: `cp .env.example .env`
- Windows (PowerShell): `Copy-Item .env.example .env`

לדמו אין צורך לשנות את הקובץ.

### צעד 3: הפעלה

```
docker compose up --build
```

- בפעם הראשונה זה לוקח 3–8 דקות, כי Docker מוריד ובונה את כל הרכיבים.
- כשמופיעה שורה עם `computed P&L` ואחריה `Uvicorn running on http://0.0.0.0:8000`, השרת מוכן.
- **לא סוגרים את חלון הטרמינל.** כדי לעצור את המערכת: `Ctrl + C`.

### צעד 4: פתיחה בדפדפן

| מה | כתובת |
| --- | --- |
| הדשבורד | http://localhost:3000 |
| תיעוד ה-API, שבו אפשר גם לנסות כל פעולה | http://localhost:8000/docs |

### צעד 5: סיור במסכים

1. **Feature P&L**: טבלת רווח והפסד לכל פיצ'ר וגרף עלות מול ערך.
   - כל נקודה מתחת לקו המקווקו מייצגת פיצ'ר שעולה יותר ממה שהוא מכניס.
   - הכתום הוא Zombie, הירוק הוא Niche Anchor והכחול הוא Cash Cow.
   - הכפתורים למעלה מחליפים את מודל הייחוס (Usage-weighted, Tier-gated, Revenue at risk), את התצוגה (Contribution לעלות ישירה בלבד, Fully loaded כולל תשתית משותפת) ואת חלון הזמן.
2. **לחיצה על פיצ'ר** (למשל Legacy PDF Exporter) פותחת את דף הפיצ'ר:
   - מגמת העלויות לאורך זמן
   - הלקוחות המשלמים שמשתמשים בו
   - הקבצים שמייצרים הכי הרבה באגים
   - בפינה למעלה, הכפתור **Start safe sunset** פותח תהליך הסרה.
3. **What-if simulator**: מסמנים פיצ'רים ורואים כמה שעות הנדסה וכמה כסף משתחררים, ואילו לקוחות בסיכון.
4. **Safe Sunset**: לוח של כל תהליכי ההסרה. לוחצים על כרטיס ועוברים על השלבים לפי הסדר:
   1. **Compute blast radius**: איזה קוד אחר עדיין מייבא את הפיצ'ר.
   2. **Acknowledge call sites**: מאשרים שבעלי הקוד יודעים.
   3. **Build account dossier**: רשימת הלקוחות שמשתמשים בפיצ'ר, עם טיוטת מייל לכל אחד. לוחצים על שם לקוח כדי לראות את הטיוטה.
   4. אם יש לקוח בסיכון גבוה, מסמנים **CS sign-off**, ואז **Advance**.
   5. **Mark accounts contacted** ואז **Advance**: הדגל נכבה ללקוחות שלא משתמשים בפיצ'ר.
   6. **Turn flag fully off**: מתחילה תקופת "מבחן צעקות" של 30 יום. בכל רגע אפשר ללחוץ **Roll back**.
   7. אחרי 30 יום שקטים: **Draft cleanup PR** ואז **Advance**.
   8. אחרי שה-PR מוזג: מסמנים **PR merged** ואז **Advance**, והפיצ'ר עובר לארכיון.
5. **Feature Graph**: תור של שיוכים שהמערכת לא בטוחה בהם (פחות מ-80% ביטחון). לכל שורה בוחרים Confirm, Reassign או Reject. בסוף לוחצים **Recompute P&L**.

### פקודות שימושיות

| מה | פקודה |
| --- | --- |
| הפעלה ברקע (בלי לתפוס את הטרמינל) | `docker compose up --build -d` |
| צפייה בלוגים | `docker compose logs -f api` |
| עצירה | `docker compose down` |
| **מחיקת כל הנתונים** והתחלה מחדש | `docker compose down -v` |

---

## חלק ג׳: מעבר לנתונים של החברה שלך

### שלב 1: הגדרת הצוותים והפיצ'רים בקובץ `features.yaml`

זה הקובץ החשוב ביותר: הוא מגדיר למערכת מה זה "פיצ'ר" אצלכם.
פותחים את `features.yaml` שבתיקייה הראשית (למשל ב-VS Code), מוחקים את הדוגמה וכותבים את שלכם.

**א. צוותים.** רק שם וכמות מהנדסים, בלי שמות של אנשים:
```yaml
version: 1

teams:
  - { key: payments, name: Payments, headcount: 6 }
  - { key: growth,   name: Growth,   headcount: 4 }
  - { key: platform, name: Platform, headcount: 5 }
```

**ב. פיצ'רים.** לכל פיצ'ר כותבים איפה הקוד שלו נמצא ואיך הוא מופיע באנליטיקס:
```yaml
features:
  - key: invoice-export                         # מזהה: אותיות קטנות, ספרות ומקפים
    name: Invoice Export                        # השם שיוצג
    owner_team: payments                        # חייב להיות key מתוך teams
    launched_at: 2022-03-01                     # תאריך השקה (לא חובה)
    paths:                                      # תיקיות הקוד, יחסית לשורש הריפו
      - "services/invoices/export/**"           # ** = כל מה שבתוך התיקייה
      - "web/src/features/invoice-export/**"
    api_routes: ["/api/v1/invoices/export*"]    # לא חובה
    events: [invoice_exported]                  # שמות האירועים ב-PostHog/Mixpanel
    flag_key: invoice-export                    # Feature flag (נדרש לתהליך הסרה)
    support_tags: [invoices]                    # תגיות ב-Zendesk (לא חובה)
    tier_gate: pro                              # רק אם הפיצ'ר זמין רק ל-pro או enterprise
    replaced_by: invoice-export-v2              # לאיזה פיצ'ר להעביר לקוחות (לא חובה)

  - key: gdpr-export
    name: GDPR Export
    owner_team: platform
    regulatory: true                            # חובה רגולטורית: לעולם לא יסומן להסרה
    paths: ["services/privacy/**"]

  - key: auth-core                              # תשתית משותפת: לא נשפטת כפיצ'ר
    name: Auth
    owner_team: platform
    shared_overhead: true
    allocation_rule: proportional_usage         # בתצוגת Fully loaded העלות מתחלקת לפי שימוש
    paths: ["services/auth/**"]
```

**ג. בדיקת תקינות לפני שממשיכים.** הפקודה זמינה כשהמערכת רצה:
- Mac: `curl -X POST localhost:8000/api/v1/manifest/validate --data-binary @features.yaml`
- Windows: `curl.exe -X POST localhost:8000/api/v1/manifest/validate --data-binary "@features.yaml"`

אם חוזר `"valid": true`, הקובץ תקין. אחרת מקבלים רשימת שגיאות מדויקת.

> **טיפ:** אין צורך למפות הכול ביום הראשון. מתחילים מ-10–15 הפיצ'רים שאתם הכי חושדים בהם.
> קוד שלא מופיע באף `paths` נספר כ-"unmapped", מופיע בכרטיס **Data gaps**, ונשלח לתור ה-Feature Graph.

### שלב 2: הגדרת `.env` למצב "נתונים אמיתיים"

פותחים את `.env` ומשנים:
```
SEED_ON_START=false
DEFAULT_ORG_SLUG=yourco        # מזהה קצר: אותיות קטנות ומקפים
ORG_NAME=Your Company Ltd
```

ואז, בטרמינל, **מוחקים את נתוני הדמו** ומפעילים מחדש:
```
docker compose down -v
docker compose up --build -d
```

הארגון שלך נוצר אוטומטית מתוך `features.yaml`.
אם תשנה את `features.yaml` בהמשך, הרץ:
- Mac: `curl -X POST localhost:8000/api/v1/manifest/apply`
- Windows: `curl.exe -X POST localhost:8000/api/v1/manifest/apply`

### שלב 3: חיבור GitHub (עלות ההנדסה)

**א. יצירת טוקן לקריאה בלבד:**
1. ב-GitHub: לוחצים על תמונת הפרופיל ואז **Settings**.
2. בתחתית התפריט השמאלי: **Developer settings**, אחר כך **Personal access tokens** ואז **Fine-grained tokens**.
3. לוחצים **Generate new token** וממלאים:
   - **Token name**: `featureledger-readonly`
   - **Expiration**: 90 ימים
   - **Resource owner**: הארגון שלכם
   - **Repository access**: **Only select repositories**, ובוחרים את הריפו של המוצר.
   - **Permissions** (בתוך Repository permissions):
     - **Pull requests**: `Read-only`
     - **Contents**: `Read-only`
     - Metadata מתווסף לבד.
4. לוחצים **Generate token** ו**מעתיקים מיד**. הטוקן לא יוצג שוב.

**ב. הכנסת הטוקן לקובץ `.env`:**
```
GITHUB_TOKEN=github_pat_xxxxxxxxxxxxxxxx
```
ואז מפעילים מחדש:
```
docker compose up -d
```

> **אזהרה:** לעולם לא מכניסים את `.env` ל-git. הוא כבר מוגדר ב-`.gitignore`.

**ג. משיכת ה-PRs:**
- Mac: `curl -X POST localhost:8000/api/v1/integrations/github/sync -H "Content-Type: application/json" -d '{"repo": "your-org/your-repo"}'`
- Windows:
  ```
  curl.exe -X POST localhost:8000/api/v1/integrations/github/sync -H "Content-Type: application/json" -d '{\"repo\": \"your-org/your-repo\"}'
  ```

הסנכרון רץ ברקע. כדי לעקוב: `docker compose logs -f api`. מחכים לשורה `github sync ... merged`.

המערכת שומרת רק מטא-דאטה: נתיבי קבצים, כמות שורות וסוג ה-PR. **שמות מפתחים וקוד מקור לא נשמרים.**

### שלב 4: ייבוא לקוחות, שימוש, טיקטים ועלויות (CSV)

בתיקייה `templates/` יש ארבעה קבצים לדוגמה. פותחים אותם ב-Excel כדי לראות את הפורמט, וממלאים קבצים עם הנתונים שלכם.
**שומרים כ-CSV UTF-8.** ב-Excel: File, אחר כך Save As, ובוחרים "CSV UTF-8".

| קובץ | עמודות | מאיפה מוציאים | תדירות |
| --- | --- | --- | --- |
| `accounts.csv` | `external_ref, name, plan_tier, arr_usd, renewal_date, health, csm_email` | Stripe: Customers, ואז Export. או מ-HubSpot/Salesforce. `plan_tier` הוא אחד מ-free/starter/pro/enterprise. | פעם בחודש |
| `usage.csv` | `account_ref, feature, active_days, active_users, events, account_active_days` | PostHog/Mixpanel: דוח מקובץ לפי חברה ולפי אירוע לאותו חודש. `feature` יכול להיות key מ-features.yaml או שם אירוע. | פעם בחודש |
| `tickets.csv` | `external_id, account_ref, feature, created_at, handle_minutes` | Zendesk: Reporting, ואז Export. `feature` יכול להיות תגית מ-`support_tags`. | פעם בחודש |
| `costs.csv` | `feature, cost_type, amount_usd` | AWS Cost Explorer: Group by Tag `feature`. עלויות LLM: מדשבורד הספק. שורה בלי feature נספרת כעלות לא מתויגת. | פעם בחודש |

- `account_ref` ב-usage וב-tickets חייב להיות זהה ל-`external_ref` ב-accounts.
- `active_days` = בכמה ימים בחודש החברה השתמשה בפיצ'ר.
- `account_active_days` = בכמה ימים בחודש החברה הייתה פעילה בכלל.

**סדר הייבוא חשוב: קודם accounts.** מחליפים את `2026-10` בחודש שהנתונים מתארים.

Mac:
```
curl -X POST "localhost:8000/api/v1/import/accounts?period=2026-10" --data-binary @accounts.csv
curl -X POST "localhost:8000/api/v1/import/usage?period=2026-10"    --data-binary @usage.csv
curl -X POST "localhost:8000/api/v1/import/tickets"                 --data-binary @tickets.csv
curl -X POST "localhost:8000/api/v1/import/costs?period=2026-10"    --data-binary @costs.csv
curl -X POST localhost:8000/api/v1/jobs/compute-pnl
```

Windows: אותן פקודות, עם `curl.exe` במקום `curl`, וגרשיים סביב שם הקובץ. למשל `--data-binary "@accounts.csv"`.

- אם יש שגיאה, **שום דבר לא נכתב**, ומקבלים רשימה כמו `line 14: active_days must be between 0 and 31`. מתקנים ומריצים שוב.
- ייבוא חוזר לאותו חודש מחליף את הנתונים הקודמים של אותו חודש.
- דרך נוחה יותר לבדוק: http://localhost:8000/docs, ואז הפעולה `POST /api/v1/import/{kind}`, ואז **Try it out**.

> **חשוב לדעת:**
> - כל עוד לא ייבאת לקוחות ושימוש, כל הפיצ'רים יסומנו **Watch**. המערכת לא קובעת ש"פיצ'ר מת" רק לפי עלות.
> - סיווג **Zombie** דורש לפחות **חודשיים** של נתונים. כדאי לייבא 3 חודשים אחורה.

### שלב 5: עבודה שוטפת (פעם בחודש)

1. מייבאים את ארבעת קובצי ה-CSV של החודש שעבר.
2. מריצים סנכרון GitHub (שלב 3ג).
3. `curl -X POST localhost:8000/api/v1/jobs/compute-pnl`
4. נכנסים ל-**Feature Graph** ומאשרים שיוכים פתוחים. ככל שתאשר יותר, הנתונים מדויקים יותר.
5. בדשבורד: מסננים **Zombie**, בודקים בסימולטור ופותחים תהליך **Safe Sunset** לפיצ'ר המתאים.

---

## חלק ג׳2: חיבור אוטומטי למקורות (Phase 2), במקום קובצי CSV

המערכת מתחברת לבד ל-**Stripe, Jira, PostHog, Zendesk ו-AWS** ומסנכרנת כל 15 דקות.

### בדמו (ברירת המחדל): בלי חשבונות בתשלום
- ב-`.env` מוגדר `SANDBOX_MODE=true`. החיבורים קוראים מ-API לדוגמה שמחקה את הספקים האמיתיים.
- אחרי ההפעלה נכנסים למסך **Integrations** (בתפריט משמאל). תוך כדקה כל חמשת המקורות עוברים ל-**Healthy**, והטבלה במסך הראשי מתמלאת.
- הכפתור **Trigger sync** מריץ סנכרון מיידי.

### חיבור אמיתי: מה לרשום ב-`.env`
משנים ל-`SANDBOX_MODE=false`, וממלאים רק את המקורות שיש לכם. מקור שלא ממלאים מופיע כ-*Not configured* ולא מפריע לשאר.

| מקור | איפה מוציאים את המפתח | מה לרשום ב-`.env` |
| --- | --- | --- |
| **Stripe** | Developers, ואז API keys, ואז **Create restricted key**. נותנים הרשאת Read ל-Customers, Subscriptions, Invoices ו-Events | `STRIPE_API_KEY=rk_live_...` |
| **Jira** | https://id.atlassian.com/manage-profile/security/api-tokens, ואז **Create API token** | `JIRA_BASE_URL=https://yourco.atlassian.net`<br>`JIRA_EMAIL=you@yourco.com`<br>`JIRA_API_TOKEN=...`<br>`JIRA_PROJECTS=ENG,APP` (לא חובה) |
| **PostHog** | Settings, ואז Personal API keys, ואז **Create**, עם הרשאות query:read ו-group:read. מספר הפרויקט מופיע בכתובת `/project/<מספר>` | `POSTHOG_PROJECT_ID=12345`<br>`POSTHOG_PERSONAL_API_KEY=phx_...` |
| **Zendesk** | Admin Center, אחר כך Apps and integrations, ואז Zendesk API. מפעילים Token access ולוחצים **Add API token** | `ZENDESK_SUBDOMAIN=yourco`<br>`ZENDESK_EMAIL=you@yourco.com`<br>`ZENDESK_API_TOKEN=...` |
| **AWS** | IAM: יוצרים משתמש עם הרשאה `ce:GetCostAndUsage`. ב-Billing, תחת Cost allocation tags, מפעילים את התג `feature` | `AWS_ACCESS_KEY_ID=...`<br>`AWS_SECRET_ACCESS_KEY=...` |

אחר כך:
```
docker compose up -d --build
```
ונכנסים למסך **Integrations**:
- הסנכרון הראשון מושך 6 חודשים אחורה (אפשר לשנות ב-`CONNECTOR_BACKFILL_MONTHS`).
- ליד כל מקור רואים סטטוס, זמן הסנכרון האחרון, היכן הסנכרון עצר (cursor), וכמה פריטים עובדו או נכשלו.

**כדי שהמערכת תדע לאיזה פיצ'ר כל נתון שייך, צריך לוודא ש:**
- ב-PostHog, האירועים (events) מופיעים תחת `events` ב-`features.yaml`, והאירועים נשלחים עם group של חברה (group type 0).
- ב-Zendesk, הטיקטים מתויגים בתגית ששווה ל-key של הפיצ'ר, או לתגית שמופיעה ב-`support_tags`.
- ב-AWS, משאבי הענן מתויגים `feature=<key>`.
- ב-Jira, ה-PRs כוללים את מפתח הכרטיס (למשל `ENG-123`) בכותרת או בתיאור.

**פרטיות:** מ-Jira לא נשמרים שמות אנשים או טקסט. מ-Zendesk לא נשמרים פונים, נציגים או שרשור השיחה. מ-Stripe נשמר רק הדומיין של המייל.

### Webhooks (לא חובה, לעדכון מהיר יותר)
בכל מקור מגדירים webhook לכתובת:
`https://<הכתובת-שלכם>/api/v1/webhooks/<stripe|jira|zendesk>/<מזהה-החיבור>`
את מזהה החיבור רואים ב-`http://localhost:8000/api/v1/integrations`. ב-`.env` רושמים את הסוד של כל webhook: `STRIPE_WEBHOOK_SECRET`, `JIRA_WEBHOOK_SECRET` ו-`ZENDESK_WEBHOOK_SECRET`.
גם בלי webhooks הכול מתעדכן כל 15 דקות.

---

## חלק ד׳: פתרון תקלות

| בעיה | פתרון |
| --- | --- |
| `Cannot connect to the Docker daemon` | Docker Desktop לא פתוח. פותחים אותו ומחכים ל-Engine running. |
| `port is already allocated` (5432, 8000 או 3000) | תוכנה אחרת תופסת את הפורט. סוגרים אותה, או משנים בקובץ `docker-compose.yml` את הפורט השמאלי, למשל `"5433:5432"`. |
| בדשבורד מופיע "Could not load the ledger" | ה-API עוד עולה. מחכים דקה, ובודקים `docker compose logs api`. |
| `unknown organization` | `DEFAULT_ORG_SLUG` ב-`.env` השתנה אחרי ההפעלה הראשונה. מריצים `docker compose down -v` ומפעילים מחדש. |
| `features in features.yaml but not in the database` | מריצים `manifest/apply` (שלב 2). |
| `account_ref(s) not found` | קודם מייבאים accounts, ורק אחר כך usage. |
| GitHub sync לא מביא כלום | בודקים שהטוקן מורשה לריפו הנכון ושה-`paths` ב-features.yaml תואמים לנתיבים האמיתיים בריפו. |
| הכול מסומן Watch | חסרים נתוני לקוחות ושימוש, או שיש רק חודש אחד. ראו שלב 4. בדמו: מחכים דקה לסנכרון הראשון (מסך Integrations). |
| חיבור במצב Failing במסך Integrations | השגיאה מופיעה בכרטיס. לרוב המפתח שגוי או חסרה הרשאה. מתקנים ב-`.env` ומריצים `docker compose up -d`. |
| Dead letters | פריטים שנכשלו 5 פעמים. לוחצים **Show dead letters** כדי לראות את הסיבה, ואחרי התיקון לוחצים **Retry all**. |
| "the sync queue (Redis) is not reachable" | שירות ה-Redis לא עלה. מריצים `docker compose ps` ואז `docker compose up -d`. |

---

## איפה מה נמצא בקוד

| רוצה לשנות... | קובץ |
| --- | --- |
| תעריף שעת מהנדס ($95), שעות בחודש, ספי סיווג | טבלת `org_settings` בבסיס הנתונים, וברירות המחדל ב-`backend/app/core/types.py` (`OrgSettings`) |
| נוסחת עלות ההנדסה | `backend/app/core/effort.py` |
| איך מחולקת ההכנסה בין פיצ'רים | `backend/app/core/attribution.py` |
| כללי הסיווג (Zombie, Anchor...) | `backend/app/core/pnl.py`, הפונקציה `classify` |
| שלבי ההסרה והתנאים שלהם | `backend/app/core/sunset.py` |
| מסכי הדשבורד | `web/app/` |
| כל הנוסחאות בהסבר מלא | `docs/SPEC.md` |
