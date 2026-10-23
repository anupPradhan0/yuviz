# Clinic SMS templates for DLT registration

Register each template below on your DLT portal exactly as written. Carriers compare every SMS
character by character against the registered template, so any difference in the fixed text
gets the message blocked. `{#var#}` marks a variable part (at most 30 characters each).

- **Header (sender ID):** 6 letters, e.g. `MDVSTA`
- **Template type:** Service Implicit (messages about a service the patient asked for)
- **Hindi templates:** register as Unicode

If the hospital name or front-desk number changes, register the templates again with the new
fixed text before changing `clinic_settings`. The texts come from `services/clinic/notifications.py`;
any edit there must be re-registered too.

## English

| # | Event | Template |
|---|---|---|
| 1 | Booked | `Medivista Multispeciality Hospital: Appointment confirmed with {#var#} ({#var#}) on {#var#}. Booking no. {#var#}. Please arrive 15 min early. To change it, call {#var#}.` |
| 2 | Rescheduled | `Medivista Multispeciality Hospital: Your appointment (booking no. {#var#}) is now with {#var#} on {#var#}. To change it, call {#var#}.` |
| 3 | Cancelled | `Medivista Multispeciality Hospital: Your appointment with {#var#} on {#var#} (booking no. {#var#}) is cancelled.` |
| 4 | Reminder | `Medivista Multispeciality Hospital: Reminder - your appointment with {#var#} ({#var#}) is on {#var#}. Booking no. {#var#}. To change it, call {#var#}.` |

Example (template 1): `Medivista Multispeciality Hospital: Appointment confirmed with Dr. Sandeep Bhatia (Orthopaedics) on Mon 19 Oct, 9 AM. Booking no. 404061. Please arrive 15 min early. To change it, call 0120 510 8340.`

## Hindi

| # | Event | Template |
|---|---|---|
| 5 | Booked | `मेडिविस्टा मल्टीस्पेशलिटी हॉस्पिटल: {#var#} ({#var#}) के साथ आपका अपॉइंटमेंट {#var#} पक्का है। बुकिंग नं. {#var#}। कृपया 15 मिनट पहले पहुँचें। बदलाव के लिए कॉल करें: {#var#}` |
| 6 | Rescheduled | `मेडिविस्टा मल्टीस्पेशलिटी हॉस्पिटल: आपका अपॉइंटमेंट (बुकिंग नं. {#var#}) अब {#var#} के साथ {#var#} है। बदलाव के लिए कॉल करें: {#var#}` |
| 7 | Cancelled | `मेडिविस्टा मल्टीस्पेशलिटी हॉस्पिटल: {#var#} के साथ {#var#} का आपका अपॉइंटमेंट (बुकिंग नं. {#var#}) रद्द कर दिया गया है।` |
| 8 | Reminder | `मेडिविस्टा मल्टीस्पेशलिटी हॉस्पिटल: याद दिलाना — {#var#} ({#var#}) के साथ आपका अपॉइंटमेंट {#var#} है। बुकिंग नं. {#var#}। बदलाव के लिए कॉल करें: {#var#}` |

Example (template 5): `मेडिविस्टा मल्टीस्पेशलिटी हॉस्पिटल: डॉ. संदीप भाटिया (ऑर्थोपेडिक्स (हड्डी रोग)) के साथ आपका अपॉइंटमेंट 19 अक्टूबर, सुबह 9 बजे पक्का है। बुकिंग नं. 404061। कृपया 15 मिनट पहले पहुँचें। बदलाव के लिए कॉल करें: 0120 510 8340`

## Which template is sent

A patient gets the Hindi template when they spoke Hindi or Hinglish on the call, otherwise English.
