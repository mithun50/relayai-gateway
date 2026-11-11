# Releasing

Both packages are called `relayai-gateway`, are released together, and share one **date-based version**: `YYYY.M.D`, the day of the release, no zero padding (`2026.11.11`, `2027.1.5`). At most one release per day.

Releases are published by CI ([`.github/workflows/release.yml`](https://github.com/mithun50/relayai-gateway/blob/main/.github/workflows/release.yml)) when a tag `v<version>` is pushed.

## Every release

```bash
# 1. all changes are listed under "## [Unreleased]" in CHANGELOG.md
# 2. set today's date as the version in both packages and date the changelog
python scripts/release_version.py
python scripts/release_version.py --check

# 3. commit, tag, push
git commit -am "Release 2026.11.11"
git tag v2026.11.11
git push origin main v2026.11.11
```

The workflow then:

1. checks that the tag matches both package versions and that [CHANGELOG.md](https://github.com/mithun50/relayai-gateway/blob/main/CHANGELOG.md) has a section for it;
2. runs the full test matrix (Linux, macOS, Windows; Python 3.8 to 3.13; Node 18 to 22);
3. builds the wheel, the sdist and the npm tarball;
4. publishes to PyPI (trusted publishing) and to npm (with provenance);
5. creates a GitHub release with the changelog notes and the built files attached.

Watch it with `gh run watch` or on the repository's Actions tab. If one of the publish jobs fails (for example an expired npm token), fix the cause and use **Re-run failed jobs**; the package that was already published is not published twice.

## One-time setup

**PyPI (trusted publishing, no token stored anywhere):**

1. Sign in at https://pypi.org (create the account and turn on two-factor authentication if needed).
2. Open https://pypi.org/manage/account/publishing/ and add a **pending publisher**:
   - PyPI project name: `relayai-gateway`
   - Owner: `mithun50`
   - Repository name: `relayai-gateway`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
3. The first successful release creates the project on PyPI and turns the pending publisher into a normal one.

**npm:**

1. Sign in at https://www.npmjs.com (two-factor authentication on).
2. Create a token: profile picture, **Access Tokens**, **Generate New Token**, **Granular Access Token**, with **Read and write** permission for packages (for the very first release it must be allowed to create new packages, so choose "All packages"), and an expiry date you will remember.
3. Store it as a repository secret:

   ```bash
   gh secret set NPM_TOKEN --repo mithun50/relayai-gateway
   ```

   (paste the token when asked)

After the first release you can narrow the token to the `relayai-gateway` package, or switch npm to trusted publishing in the package settings on npmjs.com and drop the token.

## Releasing by hand (fallback)

```bash
cd python && rm -rf dist && python -m build && python -m twine upload dist/*    # PyPI API token
cd ../node && npm publish --access public                                      # after `npm login`
gh release create v<version> --notes-file <notes> python/dist/*
```
