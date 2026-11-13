---
description: Interview the user for every detail, role, location and application answer, set up their resumes, and fill in ApplyPilot setup/config/ before anything runs.
---

Set this job-search kit up for the person you're talking to. All paths are inside `ApplyPilot setup/`. Nothing is scanned or applied until this is finished and `check_config.py` passes.

## 1. See what's missing

Run `python3 "ApplyPilot setup/check_config.py" --json`. If `ready` is true, say setup is complete, show a one-line summary, and ask whether they want to change anything. Otherwise continue, and skip any group below that's already filled in.

## 2. Ask everything, a group at a time

Ask in plain language, one group per message, and number the questions so they can answer quickly. Offer the template's defaults where sensible. Never fill in a value they didn't give you; if they skip something, leave it empty and list it at the end.

1. **Base resume.** Ask for the path to their resume (PDF or Word). Copy it to `resumes/resume.pdf` (keep the extension if it's .docx and use that path). Read it and write a plain-text copy to `resumes/resume.txt`. This is the base resume: every later answer and every tailored version must agree with it.
2. **Contact.** Full name, preferred first name, email, phone, street address, city, state, postal code, country, LinkedIn URL, and GitHub or portfolio if any. Pre-fill from the resume and ask them to confirm.
3. **Roles.** Every job title they want to apply for, including close variants (for example "Product Manager", "Technical Product Manager", "Product Owner"). Then: seniority levels to skip (intern, junior, senior, director...), title words to never apply to, and companies to avoid.
4. **Locations.** Every place they'd work: remote (and in which country), nationwide, specific cities or states, and the order they prefer them in. Whether to keep US-only roles. Their time zone.
5. **Work authorization.** Authorized to work in the country they're applying in? Need visa sponsorship now or later? Visa or permit type and expiry, if any. Citizenship or status, only if they want forms to state it.
6. **Experience and education.** Current title and company, years of relevant experience, highest degree, school, graduation year, certifications, languages.
7. **Pay and availability.** Salary expectation and range, minimum acceptable pay, full-time / contract / both (W2 or C2C), hourly rate for contracts, notice period, earliest start date, relocation, remote / hybrid / on-site, travel they'd accept.
8. **Common form questions.** Are they 18 or over? Security clearance? Previously worked at a company (answer per company, so default No)? Referred by anyone? Bound by a non-compete? Former government employee? OK with a background check and a drug test? Driver's license? How did they hear about the job (default "Job board")? Recruiting texts allowed? Pronouns (optional)?
9. **Voluntary EEO questions.** Gender, race/ethnicity, veteran status, disability. Default to "Decline to self-identify" unless they choose otherwise.
10. **Facts the agents may use.** From the base resume, draft 5-10 one-line highlights with real results (numbers exactly as written) and a skills list. Show them; keep only what they confirm. Ask for words or claims the agents must never use (a tool they haven't used, an old title).
11. **Volume and email.** Daily application target, and the Gmail address they apply from.

Then read all the answers back as one short list and ask: "Is everything here correct?" Fix anything they change.

## 3. Resumes: one generic, or one per role?

Ask: "Do you want to apply with your one resume for every job, or have me write a tailored version for each type of role you listed?"

- **One resume:** use the base resume for everything (`resumes.default`, no variants). Done.
- **Tailored versions:** group their roles into 2-4 families (for example Product Manager, Product Owner, Business Analyst) and confirm the grouping. Then for each family:
  1. Get real job descriptions for it. If a scan has run, use `python3 "ApplyPilot setup/resume_tools/sample_jds.py" "<title pattern>"` and `... --terms`. If not, ask them to paste two or three job descriptions or links, or offer to finish setup, run a first scan, and come back.
  2. Rewrite the base resume for that family: a headline and summary aimed at the role, the most relevant bullets first, wording that matches the descriptions' terms where the base resume genuinely supports them, and skills reordered. Keep every employer, title, date, school and number exactly as in the base. Do not add experience, tools, results or responsibilities that aren't in the base resume.
  3. Save it as `resumes/resume_<family>.txt` (same text layout as the base), then run `python3 "ApplyPilot setup/resume_tools/check_facts.py" resumes/resume.txt resumes/resume_<family>.txt`. Remove anything it flags, unless the person confirms it's true; then add it to the base resume too.
  4. Make the PDF: `python3 "ApplyPilot setup/resume_tools/to_pdf.py" resumes/resume_<family>.txt`. Keep it to one page if the base is one page.
  5. Show them the tailored version (or the differences from the base) and get a yes before using it.
  6. Add it to `resumes.variants` in `config/settings.json` with a `title_pattern` that matches that family's titles. More specific families go first, because the first match wins.

## 4. Write the files

Start each from its template so the structure matches what the code expects:

- `config/profile.json` from `profile.example.json`: contact, work authorization, availability, compensation, experience, skills, resume facts, EEO answers.
- `config/candidate_facts.json` from `candidate_facts.example.json`: confirmed facts, highlights, skills, `never_say`, and every `answers` entry from groups 5-9. Set `answers_confirmed` to `true` only after they confirmed the read-back in step 2.
- `config/settings.json` from `settings.example.json`: time zone, daily target, score minimums, `title_exclude`, `title_exclude_levels`, `excluded_companies`, `salary_minimum`, `region_priority` (their locations in order), `us_only`, `need_sponsorship`, `gmail_address`, `scan_title_keep` (a pattern covering all their roles), and `resumes`. Turn their words into simple case-insensitive patterns and explain them in plain language.
- `config/searches.yaml` from `searches.example.yaml`: one `query` per role from group 3, and one `location` per place from group 4 (remote ones with `remote: true`).

## 5. API keys

Tell them to copy `config/env.example.txt` to `config/env.txt` and paste at least one key (Gemini or Groq have free tiers). Don't ask them to paste keys into the chat. Wait for them to say it's done.

## 6. Check and finish

1. Run `python3 "ApplyPilot setup/check_config.py"` and fix anything it lists, asking them where needed.
2. Show a short summary: who they are, every role and location, their filters, the key answers, and which resume goes to which titles. Ask them to confirm.
3. Ask before running `bash "ApplyPilot setup/setup.sh"`. It installs ApplyPilot into `~/.applypilot-venv` and copies their settings to `~/.applypilot`. Run it only on a yes.
4. Tell them the next steps from `README.md`: scan and score jobs, build the queue, open the dashboard, and apply with `auto/apply_task_prompt.md`. If they chose tailored resumes but had no job descriptions yet, remind them to come back after the first scan.

Never commit or share anything in `config/` (other than the templates), `resumes/` or `Applications/`.
