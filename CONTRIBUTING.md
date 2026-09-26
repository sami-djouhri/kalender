# Contributing

Thanks for your interest. Please read this before you open a pull request, and
in particular the part about rights: **a contribution can only be merged once
you have agreed to the terms below.** That is not bureaucracy for its own sake,
and the reason is given plainly further down.

The codebase is written in German (identifiers, comments, user-facing text).
Issues and pull requests in English are welcome.

## Before you write code

Open an issue first if the change is more than a fix. This project has opinions
about how things are built, and it is no fun to write something that then gets
turned down for a reason you could not have known.

## Contributor License Agreement

By submitting a contribution you confirm the following.

1. **The work is yours.** You wrote it, or you have the right to submit it. If
   your employer holds rights to work you do, you have their permission.

2. **You grant a licence that is not limited to the AGPL.** You grant Sami
   Djouhri a perpetual, worldwide, non-exclusive, royalty-free, irrevocable
   licence to reproduce, modify, publish and distribute your contribution, **and
   to license it to others under any terms**, including terms that differ from
   the AGPL.

3. **You keep your copyright.** Nothing here transfers ownership. You may use
   your own contribution however you like. This is a licence to us, not a
   handover.

4. **Patents.** If your contribution is covered by a patent you control, you
   grant the same perpetual, worldwide, royalty-free licence to that patent, to
   the extent needed to use the contribution.

5. **No warranty.** You provide the contribution as-is.

### Why point 2 exists, in plain words

This project is published under the AGPL, and it will stay available under the
AGPL. But it also needs to be able to earn money one day, in two ways that are
normal for software like this: selling a commercial licence to companies that do
not want to publish their own source, and running a hosted instance.

Both require that the rights to the code stay in one pair of hands. The moment a
contribution is merged without point 2, the rights to that piece belong to its
author, and from then on **every** such decision needs the agreement of every
person who ever contributed. Projects have been stuck that way permanently. This
is the one thing that cannot be fixed later, which is why it is asked for
up front rather than when it becomes relevant.

If you are not comfortable with point 2, that is a reasonable position. Open an
issue describing the change instead; a description is not a contribution in this
sense, and a good bug report is worth as much as a patch.

### How to agree

Add a `Signed-off-by` line to each commit, using your real name and an address
you can be reached at:

```
git commit -s -m "..."
```

The sign-off means you agree to the terms above as they stand in this file at
the time of your commit.

## What gets merged

- Code in the style of what is around it. Comments explain *why*, not *what*.
- Tests for anything with a rule in it. Not for markup.
- German for user-facing text; no em dashes anywhere, including comments.
- No emoji in the interface. There is an icon registry in `packages/ui`.
