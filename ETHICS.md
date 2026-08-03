# Using this responsibly

This tool makes it easy to put numbers next to people's names. That is exactly
why it needs a note on how not to misuse it.

## What this measures

Public GitHub activity in a date window, with generated and vendored content
separated out. That is all.

## What it does not measure

Impact. Judgment. Mentoring. Incident response. On-call load. Design work.
Debugging that ended in a two-line fix. Code review depth. The teammate who
talked three people out of a bad architecture in a hallway.

For most teams, public GitHub is a **minority** of an engineer's contribution.
For teams working on private repos it may be nearly none of it.

## The one section about the team, not individuals

`ownership.py` reports bus-factor risk: subsystems where one person wrote most of
the commits. That is a **staffing finding**. It tells you where to pair someone
in or spread the next project. It is not a credit to award and not a problem to
raise with the person who happens to be the sole author. If you use it to praise
or criticize an individual you have inverted its purpose.

## Rules of use

1. **Never rank people by line count.** Volume rankings order activity, not
   value. A ten-line fix in a code generator can outweigh a 3,000-line feature.
2. **Low numbers are a question, not a finding.** Ask the person where their
   time went before forming any view. The answer is usually somewhere GitHub
   cannot see.
3. **Never paste a ranking into a review, a promo packet, or a stack-rank
   without the caveats attached.** The output carries them for a reason.
4. **Resolve identity first.** A GitHub login is not a person. Confirm the
   mapping, and never infer someone's pronouns, gender, or personal attributes
   from a name or handle.
5. **Be as skeptical of high numbers as low ones.** Every classification bug
   found while building this made someone look *better*, which is precisely why
   nobody questioned them.
6. **Tell people you are doing this.** Running an activity scan on your reports
   without their knowledge is surveillance, not management. Most engineers are
   glad to have their work found; nobody likes discovering it was measured
   behind their back.
7. **Review depth is not a productivity target.** `inline_comments_per_pr`
   exists to stop a rubber stamp counting the same as a real review. Announcing
   it as a goal turns it into a comment-padding contest and makes reviews worse.
8. **Trajectory is not a verdict.** A declining half can mean parental leave, an
   on-call rotation, a long incident, or one big project landing on the other
   side of the split. Know the context before reading the arrow.
9. **Private-repo output is confidential.** `--visibility all` puts private repo
   names and PR titles on your disk. The `.gitignore` keeps them out of git;
   keeping them out of Slack is on you.

## Not for

Stack ranking, PIP justification, layoff selection, or any use where a number
substitutes for a manager's judgment about a person's contribution. If you need
evidence for a hard conversation, the evidence is the work itself, which means
reading the PRs.
