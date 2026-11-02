// Operator CLI: npm run plan -- <userId> <free|pro>
import { setPlan, type Plan } from './db.ts';

const [userId, plan] = process.argv.slice(2);
if (!userId || (plan !== 'free' && plan !== 'pro')) {
  console.error('usage: npm run plan -- <userId> <free|pro>');
  process.exit(2);
}
if (!setPlan(userId, plan as Plan)) {
  console.error(`user ${userId} not found`);
  process.exit(1);
}
console.log(`${userId} -> ${plan}`);
