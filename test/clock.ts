// Preloaded into server processes under test (`node --import`): starts the wall clock at TEST_NOW.
const start = Date.parse(process.env.TEST_NOW ?? '');
if (Number.isFinite(start)) {
  const RealDate = Date;
  const bootedAt = RealDate.now();
  const now = () => start + (RealDate.now() - bootedAt);
  globalThis.Date = class extends RealDate {
    constructor(...args: []) {
      if (args.length) super(...args);
      else super(now());
    }
    static now = now;
  } as DateConstructor;
}
