"""Tests for the application tracker.  Run:  python3 -m unittest discover -s "ApplyPilot setup/tracker" -v"""
import csv
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mail_status as ms  # noqa: E402
import resumes  # noqa: E402
from common import Paths  # noqa: E402

LOG = [
    ["date", "company", "title", "status", "notes"],
    ["2026-10-20", "Acme Health, Inc.", "Product Manager", "submitted", ""],
    ["2026-10-15", "Elevate", "Product Manager, Payments", "submitted", "approved resume"],
    ["2026-10-15", "Halcyon", "Technical Product Manager (Remote)", "submitted", "used Resume_Technical_PM"],
    ["2026-10-25", "Quillo", "Product Manager", "skipped", "not a fit"],
    ["2026-10-26", "Vantage Systems", "Mid-level Product Manager", "submitted", ""],
    ["2026-10-26", "Lumen Media", "Senior Technical Product Manager", "FILLED - needs you", ""],
]


def mail(i, frm, subject, body, when="Mon, 26 Oct 2026 08:00:00 -0700"):
    return {"id": f"m{i}", "thread_id": f"t{i}", "date": when, "from": frm, "subject": subject, "body": body,
            "url": f"https://mail.google.com/mail/u/0/#all/t{i}"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.paths = Paths(self.tmp)
        self.paths.apps.mkdir(parents=True)
        with open(self.paths.log, "w", newline="") as fh:
            csv.writer(fh).writerows(LOG)
        self.paths.approved.parent.mkdir(parents=True)
        self.paths.approved.write_bytes(b"approved")
        self.js = self.tmp.parent / (self.tmp.name + "-job-search")
        self.js.mkdir()
        (self.js / "Jordan_Example_Resume_Technical_PM.pdf").write_bytes(b"tech")

    def tearDown(self):
        shutil.rmtree(self.tmp)
        shutil.rmtree(self.js)


class Resumes(Base):
    def test_record_snapshots_the_exact_file(self):
        row = resumes.record("Acme Health", "Product Manager", "https://x", self.paths.approved, "Claude", paths=self.paths)
        self.assertEqual(row["resume"], "Default resume")
        snap = self.tmp / row["file"]
        self.paths.approved.write_bytes(b"edited later")  # changing the resume afterwards...
        self.assertEqual(snap.read_bytes(), b"approved")  # ...doesn't change what was recorded

    def test_record_defaults_to_approved_and_says_so(self):
        row = resumes.record("Acme Health", "Product Manager", paths=self.paths)
        self.assertEqual(row["resume"], "Default resume")
        self.assertIn("didn't say", row["source"])

    def test_same_application_and_file_logged_once(self):
        resumes.record("Acme", "PM", resume=self.paths.approved, paths=self.paths)
        self.assertIsNone(resumes.record("Acme", "PM", resume=self.paths.approved, paths=self.paths))
        self.assertEqual(len(resumes.rows(self.paths)), 1)

    def test_backfill_uses_notes_then_the_default(self):
        added, unknown = resumes.backfill(self.paths, extra_dirs=[self.js])
        self.assertEqual((added, unknown), (4, 0))  # 4 submitted rows; skipped/filled ones ignored
        got = {r["company"]: r for r in resumes.rows(self.paths)}
        self.assertEqual(got["Halcyon"]["resume"], "Technical_PM")
        self.assertIn("from the notes", got["Halcyon"]["source"])
        self.assertEqual(got["Elevate"]["resume"], "Default resume")
        self.assertEqual(got["Acme Health, Inc."]["resume"], "Default resume")  # nothing in the notes: the default
        self.assertIn("default resume", got["Vantage Systems"]["source"])
        self.assertEqual(resumes.backfill(self.paths, extra_dirs=[self.js]), (0, 0))  # idempotent

    def test_lookup(self):
        resumes.backfill(self.paths, extra_dirs=[self.js])
        self.assertEqual([r["company"] for r in resumes.lookup("halcyon", self.paths)], ["Halcyon"])
        self.assertEqual(len(resumes.lookup("product manager", self.paths)), 4)


class Classify(unittest.TestCase):
    CASES = [
        ("rejected", "Your application to Acme", "Thank you for applying. Unfortunately, we have decided to move forward with other candidates whose experience more closely matches."),
        ("rejected", "Update on your candidacy", "After careful review we will not be moving forward with your application at this time. We encourage you to apply again."),
        ("rejected", "Product Manager role", "We regret to inform you that the position has been filled."),
        ("rejected", "Thanks from Elevate", "Although your background is impressive, you were not selected for an interview."),
        ("interview", "Interview invitation - Product Manager", "We'd like to invite you to an interview. Please share your availability for next week."),
        ("interview", "Next steps", "Hi Jordan, I'd love to chat. Please pick a time here: https://calendly.com/recruiter/30min"),
        ("interview", "Acme recruiter", "Can we schedule a 30 minute phone screen with the hiring manager?"),
        ("assessment", "Next step: HackerRank assessment", "Please complete the assessment within 3 days."),
        ("assessment", "Acme: one-way video interview", "Complete your HireVue interview by October 30, 2026."),
        ("offer", "Offer", "We are pleased to extend an offer for the Product Manager position."),
        ("consideration", "Thank you for applying to Acme", "We have received your application and our team is reviewing it."),
        ("consideration", "Your application was sent to Vantage Systems", "Your application was sent. Good luck!"),
        # found in the real inbox on 2026-10-26
        ("rejected", "Thank you for your interest - Product Owner", "We've reviewed your application and have decided to move forward with other"),
        ("rejected", "Amazon application: Status update", "After careful consideration and review of your application, we have decided to progress with other candidates for this role."),
        ("rejected", "Application Update – Senior PM at Tallyhub", "Our talent team reviewed your background, and we've decided not to move forward at this time."),
        ("done", "Thank you for completing the Amazon Online Assessment!", "This email serves as confirmation that you have successfully submitted your assessment."),
        ("consideration", "We’ve received your interview availability", "This is a confirmation that we've received your interview availability."),
        ("consideration", "Basis - Technical Product Manager - Confirmation of your application", "Our team has signaled your interest, and we're keen to guide you through the next steps of the interviewing process. We will review your application."),
        ("interview", "Jordan Example, Amazon wants to Interview you!", "We reviewed the resume you sent."),
        ("interview", "Jordan, you're set for your phone interview!", "Your phone interview is confirmed! October 29, 2026 at 10:00AM"),
        ("assessment", "Next Steps for Your Contoso Application", "The next step in our process is a quick Rembrandt Personality Assessment. Please complete the assessment within 3 business days."),
        (None, "Your weekly digest", "Here are 10 product manager jobs you may be interested in."),
        (None, "Lunch on Friday?", "Want to grab lunch?"),
    ]

    def test_cases(self):
        for want, subject, body in self.CASES:
            with self.subTest(subject=subject):
                self.assertEqual(ms.classify({"subject": subject, "body": body, "from": "a@b.com"})[0], want)

    def test_job_alerts_are_not_consideration(self):
        e = {"from": "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>", "subject": "Product Manager: Acme and 30 more",
             "body": "Thank you for your interest. Apply now."}
        self.assertIsNone(ms.classify(e)[0])


class Deadlines(unittest.TestCase):
    REF = "Mon, 26 Oct 2026 08:00:00 -0700"

    def check(self, body, want):
        got, _ = ms.deadline({"date": self.REF, "subject": "", "body": body})
        self.assertEqual(got, want, body)

    def test_formats(self):
        self.check("Please complete it by October 30, 2026.", date(2026, 10, 30))
        self.check("The link expires on Nov 2.", date(2026, 11, 2))
        self.check("Deadline: 11/04/2026", date(2026, 11, 4))
        self.check("Submit before Friday, October 30th.", date(2026, 10, 30))
        self.check("Please complete the assessment within 3 days.", date(2026, 10, 29))
        self.check("You have 72 hours; complete it within 72 hours.", date(2026, 10, 29))
        self.check("Complete within 5 business days.", date(2026, 11, 2))  # Mon Oct 26 + 5 working days

    def test_business_days_from_a_sunday(self):
        got, _ = ms.deadline({"date": "2026-10-25T22:15:48Z", "subject": "", "body": "Please complete the assessment within 3 business days."})
        self.assertEqual(got, date(2026, 10, 28))  # Contoso's email: Sun Oct 25 (local) -> Wed Oct 28
        self.check("No deadline mentioned here.", None)

    def test_january_written_in_december_is_next_year(self):
        got, _ = ms.deadline({"date": "Tue, 15 Dec 2026 09:00:00 -0700", "subject": "", "body": "due by Jan 5"})
        self.assertEqual(got, date(2027, 1, 5))


class Matching(Base):
    def apps(self):
        from common import submitted_apps
        return submitted_apps(self.paths)

    def test_sender_name_subject_and_domain(self):
        a = self.apps()
        self.assertEqual(ms.match(mail(1, "Acme Health Hiring Team <no-reply@us.greenhouse-mail.io>", "Update", "x"), a)["company"], "Acme Health, Inc.")
        self.assertEqual(ms.match(mail(2, "no-reply@ashbyhq.com", "Your application at Halcyon", "x"), a)["company"], "Halcyon")
        self.assertEqual(ms.match(mail(3, "Recruiting <jobs@vantagesystems.com>", "Hello", "x"), a)["company"], "Vantage Systems")

    def test_common_word_company_needs_more_than_a_body_mention(self):
        a = self.apps()
        # "elevate" in an unrelated email body must not match the company Elevate
        self.assertIsNone(ms.match(mail(4, "Streamly <info@streamly.com>", "Your plan", "Elevate your plan today"), a))

    def test_unsubmitted_jobs_are_not_matched(self):
        self.assertIsNone(ms.match(mail(5, "Quillo Recruiting <r@quillo.com>", "Thanks", "x"), self.apps()))


class Ingest(Base):
    def run_batch(self, emails):
        return ms.ingest(emails, self.paths)

    def test_end_to_end_status_deadline_resume_and_digest(self):
        resumes.backfill(self.paths, extra_dirs=[self.js])
        events = self.run_batch([
            mail(1, "Acme Health Recruiting <jobs@acmehealth.com>", "Thank you for applying to Acme Health", "We received your application.", "Fri, 23 Oct 2026 09:00:00 -0700"),
            mail(2, "Halcyon Talent <talent@halcyon.com>", "Next step: Halcyon assessment", "Please complete the HackerRank assessment by October 30, 2026."),
            mail(3, "Elevate <no-reply@hire.lever.co>", "Your application to Elevate", "Unfortunately we have decided to move forward with other candidates."),
            mail(4, "Jane at Northwind <jane@northwind.com>", "Interview invitation", "We'd like to invite you to an interview. Please share your availability."),
            mail(5, "LinkedIn <jobalerts-noreply@linkedin.com>", "30 new jobs for you", "Thank you for your interest"),
        ])
        state = ms.load(self.paths)
        st = {v["company"]: v for v in state["apps"].values()}
        self.assertEqual(st["Acme Health, Inc."]["status"], "consideration")
        self.assertEqual(st["Elevate"]["status"], "rejected")
        self.assertEqual(st["Halcyon"]["status"], "assessment")
        self.assertEqual(st["Halcyon"]["deadline"], "2026-10-30")
        self.assertEqual(st["Halcyon"]["resume"]["name"], "Technical_PM")
        # an interview for a job that isn't in the log is still tracked, under the sender's company
        self.assertEqual((st["Northwind"]["status"], st["Northwind"]["in_log"]), ("interview", False))
        self.assertEqual(state["unmatched"], [])
        self.assertEqual(len(events), 4)  # job alert ignored

        md, note = ms.digest(events, self.paths, on=date(2026, 10, 26))
        self.assertIn("ASSESSMENT** · Halcyon", md)
        self.assertIn("due 2026-10-30", md)
        self.assertIn("Resume you applied with: Technical_PM", md)
        self.assertIn("Northwind — (role not named) _(not in the application log)_", md)
        self.assertIn("assessment: Halcyon (due 10-30)", note)
        self.assertIn("interview: Northwind", note)
        self.assertTrue(self.paths.digest.exists())

    def test_same_email_is_only_counted_once(self):
        e = mail(1, "Halcyon <t@halcyon.com>", "Halcyon assessment", "Complete the assessment within 3 days.")
        self.assertEqual(len(self.run_batch([e])), 1)
        self.assertEqual(len(self.run_batch([e])), 0)

    def test_older_or_weaker_emails_dont_overwrite(self):
        self.run_batch([mail(1, "Elevate <a@elevate.com>", "Elevate update", "We regret to inform you.", "Mon, 26 Oct 2026 10:00:00 -0700")])
        # an auto-reply arriving after the rejection must not reopen it
        self.run_batch([mail(2, "Elevate <a@elevate.com>", "Thanks for applying to Elevate", "We received your application.", "Mon, 26 Oct 2026 11:00:00 -0700")])
        # an older interview email processed late must not overwrite a newer status
        self.run_batch([mail(3, "Elevate <a@elevate.com>", "Elevate interview", "Please share your availability for a phone screen.", "Thu, 22 Oct 2026 10:00:00 -0700")])
        a = next(iter(ms.load(self.paths)["apps"].values()))
        self.assertEqual(a["status"], "rejected")
        self.assertEqual(len(a["history"]), 3)

    def test_rejection_after_interview_wins(self):
        self.run_batch([mail(1, "Halcyon <t@halcyon.com>", "Halcyon interview", "Can we schedule a phone screen?", "Mon, 19 Oct 2026 10:00:00 -0700")])
        self.run_batch([mail(2, "Halcyon <t@halcyon.com>", "Halcyon update", "We will not be moving forward with your application.")])
        self.assertEqual(next(iter(ms.load(self.paths)["apps"].values()))["status"], "rejected")

    def test_past_deadlines_drop_off_the_morning_list(self):
        self.run_batch([mail(1, "Halcyon <t@halcyon.com>", "Halcyon assessment", "Complete the assessment by October 27, 2026.")])
        self.assertEqual(len(ms.upcoming(ms.load(self.paths), on=date(2026, 10, 27))), 1)
        self.assertEqual(len(ms.upcoming(ms.load(self.paths), on=date(2026, 10, 28))), 0)
        _, note = ms.digest([], self.paths, on=date(2026, 10, 28))
        self.assertEqual(note, "")  # nothing to act on -> no notification


class RealInboxCases(Base):
    """Behaviour found while testing against the real inbox on 2026-10-26."""

    def test_interview_date_and_time(self):
        e = {"date": "2026-10-21T18:12:18Z", "subject": "Jordan, you're set for your phone interview!",
             "body": "Your phone interview is confirmed! We're excited to talk to you on: October 29, 2026 at 10:00AM Eastern Time (UTC-5)"}
        d, text = ms.deadline(e, interview=True)
        self.assertEqual(d, date(2026, 10, 29))
        self.assertIn("10:00AM Eastern Time", text)

    def test_unnamed_role_goes_to_the_role_already_interviewing(self):
        with open(self.paths.log, "a", newline="") as fh:
            csv.writer(fh).writerows([["2026-10-15", "Amazon", "Product Manager - Returns", "ONLINE ASSESSMENT", ""],
                                      ["2026-10-24", "Amazon", "Product Manager III, Amazon Devices", "submitted", ""]])
        ms.ingest([mail(1, "noreply@mail.amazon.jobs", "Amazon wants to Interview you!", "We reviewed the resume you sent for our Product Manager, Returns Automation position", "Mon, 19 Oct 2026 12:00:00 -0700")], self.paths)
        # a later email that doesn't name the role must not jump to the newest Amazon application
        ms.ingest([mail(2, "noreply@mail.amazon.jobs", "Amazon phone interview on October 29, 2026 at 10:00AM", "Your phone interview is confirmed.")], self.paths)
        st = {v["title"]: v for v in ms.load(self.paths)["apps"].values()}
        self.assertEqual(len(st["Product Manager - Returns"]["history"]), 2)
        self.assertNotIn("Product Manager III, Amazon Devices", st)

    def test_email_cant_belong_to_an_application_sent_after_it(self):
        with open(self.paths.log, "a", newline="") as fh:
            csv.writer(fh).writerows([["2026-10-14", "Amazon", "Product Manager - Seller Tools", "submitted", ""],
                                      ["2026-10-24", "Amazon", "Technical PM, AWS Capacity Planning", "submitted", ""]])
        a = ms.match(mail(1, "noreply@mail.amazon.jobs", "Invitation to complete the Amazon Online Assessment", "next step", "Wed, 14 Oct 2026 12:00:00 -0700"),
                     __import__("common").submitted_apps(self.paths))
        self.assertEqual(a["title"], "Product Manager - Seller Tools")

    def test_company_guesses_for_jobs_missing_from_the_log(self):
        g = ms.guess_job
        self.assertEqual(g({"from": "zeta@myworkday.com", "subject": "Thank you for your interest - Product Owner", "body": ""})[0], "ZETA")
        self.assertEqual(g({"from": "no-reply@hire.lever.co", "subject": "Next Steps for Your Contoso Application",
                            "body": "for applying to the Senior Product Manager - Contoso Labs position."}), ("Contoso", "Senior Product Manager - Contoso Labs"))
        self.assertEqual(g({"from": "no-reply@us.greenhouse-mail.io", "subject": "Application Update – Senior PM, Billing Platform at Tallyhub", "body": ""})[0], "Tallyhub")
        self.assertEqual(g({"from": "careers@talent.fabrikam.com", "subject": "Jordan, we have received your application", "body": ""})[0], "Fabrikam")
        self.assertEqual(g({"from": "no-reply@ashbyhq.com", "subject": "We've Received Your Application - Thanks!", "body": ""})[0], "")

    def test_one_notification_line_for_several_roles_with_one_assessment(self):
        body = "Thank you for applying to the {} position. Please complete the assessment within 3 business days."
        ms.ingest([mail(i, "no-reply@hire.lever.co", "Next Steps for Your Contoso Application", body.format(t), "2026-10-25T22:15:48Z")
                   for i, t in enumerate(["Product Manager - Contoso Labs", "Senior Product Manager - Contoso Labs"])], self.paths)
        _, note = ms.digest([], self.paths, on=date(2026, 10, 26))
        self.assertEqual(note, "1 to act on — assessment: Contoso ×2 (due 10-28)")


class AssessmentCompleted(Base):
    def test_vendor_completion_email_closes_every_open_assessment_it_requested(self):
        body = "Thank you for applying to the {} position. Please complete the assessment within 3 business days."
        ms.ingest([mail(i, "no-reply@hire.lever.co", "Next Steps for Your Contoso Application", body.format(t), "2026-10-25T22:15:48Z")
                   for i, t in enumerate(["Product Manager - Contoso Labs", "Senior Product Manager - Contoso Labs"])], self.paths)
        ms.ingest([mail(5, "no-reply@mail.rembrandtadvantage.com", "Request to Complete Rembrandt Portrait Profile Assessment - Contoso Systems",
                        "Please complete your assessment.", "2026-10-27T19:00:00Z")], self.paths)
        st = ms.load(self.paths)["apps"]
        self.assertTrue(all(a["deadline"] == "2026-10-28" for a in st.values()))  # a reminder without a date keeps the deadline
        ms.ingest([mail(6, "no-reply@mail.rembrandtadvantage.com", "Assessment Completed - Confirmation Code Enclosed",
                        "Dear Jordan Example, We are pleased to inform you that your assessment has been successfully completed.",
                        "2026-10-27T20:28:19Z")], self.paths)
        st = ms.load(self.paths)["apps"]
        self.assertEqual({a["status"] for a in st.values()}, {"consideration"})
        self.assertFalse(any(a["company"].lower().startswith("rembrandt") for a in st.values()))  # vendor isn't an employer
        self.assertEqual(ms.upcoming(ms.load(self.paths), on=date(2026, 10, 27)), [])


class Today(Base):
    """Email formats that are easy to misclassify."""

    def test_receipt_that_might_schedule_a_call_is_not_an_interview(self):
        for body in ("Thanks for applying! If your experience is a match, we'll reach out to schedule an initial conversation.",
                     "Our team will review your application and, should there be a fit, we will contact you to schedule a conversation."):
            self.assertEqual(ms.classify({"subject": "Thank you for applying to Harbor Health!", "body": body, "from": "x"})[0], "consideration")
        real = "Hi Jordan, I'd like to schedule a 30 minute call with you at your earliest possible convenience."
        self.assertEqual(ms.classify({"subject": "Harbor Health next steps", "body": real, "from": "x"})[0], "interview")

    def test_company_and_title_from_receipts(self):
        self.assertEqual(ms.guess_job({"from": "no-reply@gearhauscollect.com", "body": "",
                                       "subject": "Thank you for applying to the Senior Product Manager, Marketplace role at Gearhaus Collectibles"})[0],
                         "Gearhaus Collectibles")
        self.assertEqual(ms.guess_job({"from": "no-reply@keyward.com", "subject": "Your Keyward application",
                                       "body": "Thank you for taking the time to consider the role of Senior Product Manager - Keyward Access Platform at Keyward."})[1],
                         "Senior Product Manager - Keyward Access Platform")

    def test_named_role_at_same_company_is_a_separate_application(self):
        with open(self.paths.log, "a", newline="") as fh:
            csv.writer(fh).writerow(["2026-10-24", "Bluefin Health", "Product Manager, Clinical Workflow (SF)", "submitted", ""])
        ms.ingest([mail(1, "no-reply@ashbyhq.com", "Update on your application for Senior Product Manager (Multiple Opening)",
                        "Thank you for applying to the Senior Product Manager (Multiple Opening) role at Bluefin Health. "
                        "After reviewing your application, we've decided not to move forward at this time.", "2026-10-27T18:05:33Z")], self.paths)
        st = {a["title"]: a["status"] for a in ms.load(self.paths)["apps"].values()}
        self.assertEqual(st.get("Senior Product Manager (Multiple Opening)"), "rejected")
        self.assertNotIn("Product Manager, Clinical Workflow (SF)", st)

    def test_unnamed_receipt_merges_into_the_named_rejection(self):
        ms.ingest([mail(1, "no-reply@keyward.com", "Thank you for applying to Keyward!", "We received your application.", "2026-10-24T12:00:00Z")], self.paths)
        ms.ingest([mail(2, "no-reply@keyward.com", "Your Keyward application", "Thank you for taking the time to consider the role of "
                        "Senior Product Manager - Keyward Access Platform at Keyward. We have chosen to move forward with other candidates.",
                        "2026-10-27T16:15:17Z")], self.paths)
        apps = list(ms.load(self.paths)["apps"].values())
        self.assertEqual([(a["title"], a["status"], len(a["history"])) for a in apps],
                         [("Senior Product Manager - Keyward Access Platform", "rejected", 2)])

    def test_receipt_goes_to_the_application_already_sent(self):
        with open(self.paths.log, "a", newline="") as fh:
            csv.writer(fh).writerows([["2026-10-25", "Orbit Learning", "Product Manager", "submitted", ""],
                                      ["2026-10-26", "Orbit Learning", "Senior Product Manager", "submitted", ""]])
        ms.ingest([mail(1, "no-reply@ashbyhq.com", "Thank You for Applying to Orbit Learning", "We received your application.", "2026-10-25T18:19:27Z")], self.paths)
        self.assertEqual([a["title"] for a in ms.load(self.paths)["apps"].values()], ["Product Manager"])

    def test_recruiter_threads_are_left_to_the_recruiter_tracker(self):
        (self.paths.apps / "recruiter_leads.json").write_text('{"leads": {"t9": {}}}')
        e = mail(9, "pat@consulting.example", "RE: W2 POSITION", "Thank you for your application. Halcyon Talent.", "Mon, 26 Oct 2026 10:00:00 -0700")
        self.assertEqual(ms.ingest([dict(e, thread_id="t9")], self.paths), [])


class NotifyOncePerDay(Base):
    def test_first_run_of_the_day_and_new_news_only(self):
        d1, d2 = date(2026, 10, 27), date(2026, 10, 28)
        self.assertTrue(ms.should_notify([], self.paths, on=d1))   # first run today
        self.assertFalse(ms.should_notify([], self.paths, on=d1))  # same news, 4 hours later
        self.assertTrue(ms.should_notify([{"status": "interview"}], self.paths, on=d1))  # new interview
        self.assertFalse(ms.should_notify([{"status": "rejected"}], self.paths, on=d1))  # rejections don't pop up
        self.assertTrue(ms.should_notify([], self.paths, on=d2))   # next morning


class RecordSh(Base):
    def test_record_sh_logs_the_resume_on_submission(self):
        auto = self.tmp / "auto"
        auto.mkdir()
        for f in ("record.sh", "job_url.py"):
            shutil.copy(HERE.parent / "auto" / f, auto / f)
        shutil.copytree(HERE, self.tmp / "tracker", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(HERE.parent / "settings.py", self.tmp / "settings.py")  # record.sh and the tracker read settings
        (self.tmp / "config").mkdir()
        for f in (HERE.parent / "config").glob("*.example.*"):  # templates only, never your filled-in copies
            shutil.copy(f, self.tmp / "config" / f.name)
        tech = self.js / "Jordan_Example_Resume_Technical_PM.pdf"
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.tmp), "RESUME_FILE": str(tech), "APPLY_AGENT": "Codex"}
        r = subprocess.run(["bash", str(auto / "record.sh"), "https://jobs.lever.co/x/1", "submitted", "Beta Co", "Product Manager", "ok", "--log-only"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        row = resumes.rows(self.paths)[-1]
        self.assertEqual((row["company"], row["resume"], row["agent"]), ("Beta Co", "Technical_PM", "Codex"))
        self.assertEqual((self.tmp / row["file"]).read_bytes(), b"tech")
        # skipped jobs don't get a resume row
        subprocess.run(["bash", str(auto / "record.sh"), "https://jobs.lever.co/x/2", "skipped", "Sigma Test", "PM", "no"], capture_output=True, env=env)
        self.assertEqual(len(resumes.rows(self.paths)), 1)


if __name__ == "__main__":
    unittest.main()
