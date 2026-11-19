-- Work pushed straight to the default branch, without a pull request (solo developers, small teams).
-- Such commits become work items of type 'commit'. Commits that belong to a pull request are skipped,
-- so nothing is counted twice. No author is stored, as for pull requests.
ALTER TYPE work_item_type ADD VALUE IF NOT EXISTS 'commit';
