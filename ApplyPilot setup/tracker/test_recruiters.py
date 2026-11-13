"""Tests for recruiter-outreach tracking.  Run:  python3 -m unittest discover -s "ApplyPilot setup/tracker" -v
Fixtures are shortened, anonymized recruiter emails. The tests use their own facts file, not your config."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import recruiters as rc  # noqa: E402
from common import Paths  # noqa: E402

ME = "jordan.example@example.com"
rc.ME = ME

# fixed facts for the tests, so they pass whatever is in config/candidate_facts.json
FACTS = {
    "name": "Jordan Example", "first_name": "Jordan", "email": ME, "phone": "(555) 555-0100",
    "location": "Austin, TX (open to relocation)", "zip": "78701", "linkedin": "linkedin.com/in/jordan-example",
    "current_role": "Product Manager at Example Corp", "experience": "5+ years in product management",
    "certification": "", "education": "B.S. Example, Example University",
    "answers": {
        "work_authorization": "Yes, I'm authorized to work in the US on an EAD, valid through 06/30/2030.",
        "sponsorship": "Yes, I will need sponsorship in the future.",
        "employment_type": "W2 (contract or full-time). I'm not able to work Corp-to-Corp.",
        "w2_rate": "$65/hour on W2", "salary": "$100,000, or within your posted range",
        "notice": "2 weeks' notice.", "relocation": "Open to relocation.",
        "interview_availability": "Please share a few times that work for you.",
    },
    "highlights": ["Led a single-day rollout to 50,000+ users", "Shipped features for customers running 200+ devices"],
    "skills": ["Roadmapping"], "never_say": ["enterprise", "Product Analyst"], "resume_to_attach": "resumes/resume.pdf",
}
_facts_dir = tempfile.mkdtemp()
rc.FACTS = Path(_facts_dir) / "candidate_facts.json"
rc.FACTS.write_text(json.dumps(FACTS))


def msg(i, tid, sender, subject, body, date, mine=False):
    return {"id": f"m{i}", "threadId": tid, "date": date, "sender": ME if mine else sender, "subject": subject,
            "plaintextBody": body, "viewUrl": f"https://mail.google.com/x/{tid}/{i}", "labelIds": ["SENT"] if mine else ["INBOX"]}


PITCH = ("relay-123@user.jobboard.example", "Job opening - Technical product manager",
          "Hello Jordan, I'm a recruiter at Northwind Staffing. Please let me know the best time to reach you, or you may call me directly. "
          "Job Description: Job Title: Technical Product manager Location: Austin TX Duration: 6 months (Extendable) "
          "Pay Range: $45-50/Hour on W2(All Inclusive) Request ID: 424242-1 Position Summary: Own the product strategy, roadmap, and lifecycle.")
CITIZEN_ONLY = ("kai@staffing-one.example", "AI Delivery Scrum Lead",
           "I'm reaching out regarding an opportunity for an AI Delivery Scrum Lead.\nJob Title: AI Delivery Scrum Lead\n"
           "Location: Remote – USA\nDuration: ASAP through June 2030\nWork Authorization: US Citizens (USC) / Green Card holders (GC) only\n"
           "Interested? Please reply with your updated resume, current location, work authorization.")
NOT_PRODUCT = ("maya@staffing-three.example", "Job Lead Developer is shared with you",
         "Hello Jordan Example, I am reaching out to you on an exciting job opportunity with one of our clients.\nJob Title - Lead Developer\n"
         "Location: Dallas, TX\nPlease share your updated resume.")
VR_1 = ("riley@vr.recruiting.example", "Your product ownership background made your Product Owner application stronger, Jordan",
          "Hi Jordan,\nThis is Riley, a virtual recruiter with Contoso Talent.\nJob Description:\nJob title: Product Owner\nLocation: Remote\n"
          "Duration: 6 Months to begin with\nResponsibilities: Define and manage the product roadmap. Experience with Agile methodologies.\n"
          "If you are interested, let me know a time for a call.")
VR_2 = ("riley@vr.recruiting.example", "Your product ownership background made your Product Owner application stronger, Jordan",
          "Hi Jordan,\nWe had previously shared the job description with you. To proceed, please share your updated resume and the following:\n"
          "Total Experience:\nCurrent Location (Zip Code):\nUS work authorization (Yes/No):\nWork Sponsorship needed (Yes/No):\n"
          "W2 or Corp to Corp Preferences (W2/C2C):\nExpected Salary:\nNotice Period:\nAvailability for an Interview:\n"
          "Favourite programming language:\nRegards,\nVirtual Recruiter Riley")
CONTRACT = ("pat@consulting.example", "W2 POSITION: Employment Opportunity - Senior Associate, Technical Program Management, CA",
          "Hi Jay,\nI came across your resume online and I have a position for a Senior Associate, Technical Program Management with "
          "my client, Tailspin Motors, located in San Diego, CA, that you seem to be a good fit for. Please take look and let me know!\n"
          "Position Description: Our Client is looking for an early-career Technical Program Manager. Contract W2.")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.paths = Paths(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def scan(self, *threads):
        rc.scan([{"messages": t} for t in threads], self.paths)
        return rc.load(self.paths)["leads"]


class Detection(Base):
    def test_pitch_fields_and_screen(self):
        lead = self.scan([msg(1, "t1", *PITCH, "2027-01-13T21:25:37Z")])["t1"]
        self.assertEqual((lead["title"], lead["location"]), ("Technical Product manager", "Austin TX"))
        self.assertIn("$45-50/Hour", lead["pay"])
        self.assertIn("W2", lead["type"])
        self.assertEqual((lead["status"], lead["screen"]), ("read", "passed"))

    def test_hard_screen_reasons(self):
        leads = self.scan([msg(1, "t1", *CITIZEN_ONLY, "2027-01-26T16:33:47Z")], [msg(2, "t2", *NOT_PRODUCT, "2027-01-26T18:14:08Z")])
        self.assertEqual(leads["t1"]["status"], "not_match")
        self.assertIn("US citizen / green card only", leads["t1"]["note"])
        self.assertIn("Lead", leads["t1"]["note"])
        self.assertEqual(leads["t2"]["status"], "not_match")
        self.assertIn("not a product / program", leads["t2"]["note"])

    def test_low_pay_and_c2c_only(self):
        low = (PITCH[0], PITCH[1], PITCH[2].replace("$45-50/Hour on W2", "$25-30/hr on W2"))
        c2c = ("a@agency.com", "Product Owner - C2C", "My client needs a Product Owner. Location: Remote. Rate: $70/hr C2C only. Please share your updated resume.")
        leads = self.scan([msg(1, "t1", *low, "2027-01-20T10:00:00Z")], [msg(2, "t2", *c2c, "2027-01-20T10:00:00Z")])
        self.assertIn("under your $80k floor", leads["t1"]["note"])
        self.assertIn("C2C only", leads["t2"]["note"])

    def test_receipts_alerts_and_newsletters_are_not_leads(self):
        leads = self.scan(
            [msg(1, "t1", "no-reply@us.greenhouse-mail.io", "Thank you for applying to Example Co", "We received your application. Location: Remote. Let me know", "2027-01-14T12:00:00Z")],
            [msg(2, "t2", "jobs@alerts.jobot.com", "New opportunity as a Business Analyst (MITA)", "Job title: Business Analyst. Apply now. Let me know", "2027-01-26T12:00:00Z")],
            [msg(3, "t3", "hello+all@wonsulting.com", "Your resume", "Job description tips. Share your resume", "2027-01-21T12:00:00Z")])
        self.assertEqual(leads, {})


class TrickyFormats(Base):
    """Recruiter-email formats that are easy to misread."""

    def test_job_title_field_without_an_explicit_ask_is_a_pitch(self):
        ken = ("ken@staffing-one.example", "New Requirement || Need GC or USC candidate",
                 "*Job Title:* Data Scientist – Pricing Analytics\n\n*Location: *Bay Area, CA\n\n*Client:* Litware AI\n"
                 "*Note: We only need GC or USC candidate.*\n\n*Responsibilities:*\n* Develop models.\n\nThanks & Regards,\nKen | US Recruiter")
        lead = self.scan([msg(1, "k", *ken, "2027-01-20T17:28:51Z")])["k"]
        self.assertEqual(lead["title"], "Data Scientist – Pricing Analytics")
        self.assertIn("US citizen / green card only", lead["note"])

    def test_pitch_you_already_answered_is_sent(self):
        staffing = ("dana@staffing-two.example", "Urgent Need || Senior Scrum Master || Austin, TX – Onsite",
               "This is Dana from Fabrikam IT Staffing.\n*Job Title: Senior Scrum Master *\n*Location: Austin, TX – Onsite*\n*Duration: 6-12 Months*\n"
               "o 6 to 8 years in Agile environments with a minimum of 2 to 3 years as a Scrum Master.\n"
               "If you are qualified, available, interested, please email me.")
        lead = self.scan([msg(1, "h", *staffing, "2027-01-14T20:51:11Z"),
                          msg(2, "h", "", "Re: Urgent Need", "Hi Dana, I am interested.", "2027-01-14T21:06:07Z", mine=True)])["h"]
        self.assertEqual((lead["title"], lead["status"]), ("Senior Scrum Master", "sent"))
        self.assertEqual(lead["screen"], "passed")  # "6 to 8 years" is a stretch, not a hard no

    def test_rtr_in_its_own_thread_needs_you_and_keeps_the_role(self):
        rtr = ("pat@consulting.example", "RTR Right to Represent – Senior Associate, Technical Program Management | Tailspin Motors",
               "Based on our conversations, we have agreed to submit you for the following position:\n\nClient: Tailspin Motors\n"
               "Role: Senior Associate, Technical Program Management\nLocation: San Diego, CA\n\n*RIGHT TO REPRESENT*\n"
               "Please confirm your acceptance.\nSSN Last 4 Digits:\nCurrent Visa Expiration Date: ___2030___")
        lead = self.scan([msg(1, "r", *rtr, "2027-01-27T13:20:14Z")])["r"]
        self.assertEqual(lead["title"], "Senior Associate, Technical Program Management")
        self.assertEqual(lead["status"], "needs_you")

    def test_job_details_in_first_message_and_questions_in_the_follow_up(self):
        a1 = (VR_1[0], VR_1[1], VR_1[2].replace("If you are interested, let me know a time for a call.", "I'm happy to connect."))
        lead = self.scan([msg(1, "a", *a1, "2027-01-24T22:40:23Z"), msg(2, "a", *VR_2, "2027-01-26T15:16:52Z")])["a"]
        self.assertEqual((lead["title"], lead["location"]), ("Product Owner", "Remote"))


class Conversation(Base):
    def test_already_replied_then_rtr_needs_you(self):
        t = [msg(1, "d", *CONTRACT, "2027-01-26T18:43:57Z"),
             msg(2, "d", "", "Re: W2 POSITION", "Hi Pat, thank you for reaching out.", "2027-01-26T19:17:00Z", mine=True),
             msg(3, "d", "pat@consulting.example", "RE: W2 POSITION", "$50/hr on W2 Will work? Kindly share your resume", "2027-01-26T22:54:00Z"),
             msg(4, "d", "", "Re: W2 POSITION", "Yes, $50/hr on W2 works for me.", "2027-01-26T22:58:00Z", mine=True),
             msg(5, "d", "pat@consulting.example", "RTR Right to Represent – Senior Associate, Technical Program Management", "Sending rtr", "2027-01-27T12:57:00Z")]
        lead = self.scan(t)["d"]
        self.assertEqual(lead["status"], "needs_you")
        self.assertIn("RTR", lead["note"])
        self.assertIn("San Diego", lead["location"])

    def test_full_cycle_read_match_drafted_sent_replied(self):
        first = [msg(1, "a", *VR_1, "2027-01-24T22:40:23Z")]
        self.assertEqual(self.scan(first)["a"]["status"], "read")
        state = rc.load(self.paths)
        rc.set_status(state["leads"]["a"], "match", "PO, roadmap + Agile")
        rc.set_status(state["leads"]["a"], "drafted", "ready")
        rc.save(state, self.paths)
        self.assertEqual(self.scan(first)["a"]["status"], "drafted")  # nothing new: keeps the draft
        sent = first + [msg(2, "a", "", "Re: Product Owner", "Thanks Riley", "2027-01-25T09:00:00Z", mine=True)]
        lead = self.scan(sent)["a"]
        self.assertEqual((lead["status"], lead["note"]), ("sent", "you sent the draft"))
        lead = self.scan(sent + [msg(3, "a", *VR_2, "2027-01-26T15:16:52Z")])["a"]
        self.assertEqual(lead["status"], "read")
        self.assertIn("recruiter replied", lead["note"])
        self.assertEqual(lead["reply_to"], "m3")

    def test_asks_and_skeleton_answer_only_from_approved_facts(self):
        lead = self.scan([msg(1, "a", *VR_1, "2027-01-24T22:40:23Z"), msg(2, "a", *VR_2, "2027-01-26T15:16:52Z")])["a"]
        self.assertIn("Total Experience:", lead["asks"])
        text = rc.skeleton(dict(lead, recruiter="Riley Contoso <riley@vr.recruiting.example>"), lead["asks"])
        self.assertTrue(text.startswith("Hi Riley,"))
        self.assertIn("US work authorization (Yes/No): Yes, I'm authorized to work in the US on an EAD, valid through 06/30/2030.", text)
        self.assertIn("Work Sponsorship needed (Yes/No): Yes, I will need sponsorship in the future.", text)
        self.assertIn("Current Location (Zip Code): Austin, TX (open to relocation), 78701", text)
        self.assertIn("I'm not able to work Corp-to-Corp", text)
        self.assertIn("Favourite programming language: [YOU: no approved answer]", text)  # never guessed


class Lint(unittest.TestCase):
    def test_flags_invented_numbers_banned_words_and_old_title(self):
        bad = "I'm a Product Analyst with 10 years of enterprise experience who led 40 engineers."
        probs = rc.lint(bad)
        self.assertTrue(any("10" in p for p in probs))
        self.assertTrue(any("40" in p for p in probs))
        self.assertTrue(any("Product Analyst" in p for p in probs))
        self.assertTrue(any("enterprise" in p for p in probs))

    def test_approved_facts_and_recruiters_own_numbers_pass(self):
        good = ("I led a single-day rollout to 50,000+ users and shipped features for customers running 200+ devices. "
                "$45-50/hour on W2 works for me. I'm on an EAD, valid through 06/30/2030. Call me at (555) 555-0100.")
        self.assertEqual(rc.lint(good, lead_text="Pay Range: $45-50/Hour on W2"), [])


class Notify(Base):
    def test_notifies_each_draft_once(self):
        self.scan([msg(1, "a", *VR_1, "2027-01-24T22:40:23Z")])
        state = rc.load(self.paths)
        rc.set_status(state["leads"]["a"], "drafted", "ready")
        rc.save(state, self.paths)
        self.assertIn("1 recruiter reply drafted: Product Owner", rc.notify_text(self.paths))
        self.assertEqual(rc.notify_text(self.paths), "")


if __name__ == "__main__":
    unittest.main()
