# Memoria parent-app design v4

This folder is the design reference for the WeChat mini program now, and for the native App later.

## Contents

- `all-pages.jpg` and `01`–`10` page PNGs. Static mockups at phone size.
- `app.html`. Interactive HTML source for the same ten screens.
- `design-notes.md`. Visual system, copy rules, and review notes from the design pass.
- `assets/`. Story and mascot stills used in the mockups. Archive copies are JPEG-compressed for size. Prefer regenerating from the original pipeline if you need print resolution.

## Mascot images

The mascot images in `assets/` are AI-generated (`gpt-image-2.5` via bb-api, 14 Oct 2026). Early-stage product use is allowed. Re-evaluate licensing and quality before mass production or store marketing at scale.

## Mapping to the mini program

| Design screen | Mini program area |
|---|---|
| Login | `pages/auth` |
| Guardian consent | bind / privacy consent steps |
| Home | `pages/home` |
| Conversation review | `pages/memory` (daily summaries) |
| Needs attention | home attention detail / crisis alert sheet |
| Usage limits | `pages/device` session limits |
| Content / stories | home recommendation or future content surface |
| Roles | `pages/companion` (tab name stays 「角色」) |
| Memory (three long-term facts) | `pages/memory` / child-memory |
| Profile | `pages/profile` |

Subscription WeChat push is paused in product. The mockups already drop the remaining-count card. The mini program keeps the subscribe code behind a flag that defaults off.
