const assert = require("node:assert/strict");
const test = require("node:test");

const {
  assertProtectedReleaseEnvironments,
} = require("./assert-release-environments.cjs");

const protectedEnvironment = (name) => ({
  name,
  protection_rules: [
    {
      type: "required_reviewers",
      reviewers: [{ type: "User", reviewer: { login: "release-reviewer" } }],
    },
  ],
});

test("checks only the registry selected for a partial release", async () => {
  const checked = [];

  await assertProtectedReleaseEnvironments({
    packages: "pypi",
    getEnvironment: async (name) => {
      checked.push(name);
      return protectedEnvironment(name);
    },
  });

  assert.deepEqual(checked, ["pypi"]);
});

test("checks both registries for a complete release", async () => {
  const checked = [];

  await assertProtectedReleaseEnvironments({
    packages: "both",
    getEnvironment: async (name) => {
      checked.push(name);
      return protectedEnvironment(name);
    },
  });

  assert.deepEqual(checked, ["npm", "pypi"]);
});

test("refuses an environment that does not exist", async () => {
  await assert.rejects(
    assertProtectedReleaseEnvironments({
      packages: "npm",
      getEnvironment: async () => {
        const error = new Error("Not Found");
        error.status = 404;
        throw error;
      },
    }),
    /npm.*does not exist/i,
  );
});

test("refuses an environment with no required-reviewer rule", async () => {
  await assert.rejects(
    assertProtectedReleaseEnvironments({
      packages: "npm",
      getEnvironment: async () => ({ name: "npm", protection_rules: [] }),
    }),
    /npm.*required reviewer/i,
  );
});

test("refuses a required-reviewer rule whose reviewer list is empty", async () => {
  await assert.rejects(
    assertProtectedReleaseEnvironments({
      packages: "npm",
      getEnvironment: async () => ({
        name: "npm",
        protection_rules: [{ type: "required_reviewers", reviewers: [] }],
      }),
    }),
    /npm.*required reviewer/i,
  );
});

test("refuses an unexpected package selector", async () => {
  await assert.rejects(
    assertProtectedReleaseEnvironments({
      packages: "something-else",
      getEnvironment: async () => protectedEnvironment("npm"),
    }),
    /unknown package selection/i,
  );
});
