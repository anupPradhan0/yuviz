import { redirect } from "next/navigation";

// Numbers are managed per provider configuration on Telephony.
export default function PhoneNumbersPage() {
  redirect("/telephony");
}
