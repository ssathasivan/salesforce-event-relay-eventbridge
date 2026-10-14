/**
 * Durable error log for the Member Relay triggers.
 *
 * System.debug output is lost unless a trace flag is active, so failures are
 * written to Relay_Error_Log__c instead, where you can query them with SOQL.
 *
 * "without sharing" because the platform event subscriber runs as the
 * Automated Process user, and the log insert must not depend on that user's
 * sharing access.
 */
public without sharing class RelayErrorLog {

    public static Relay_Error_Log__c entry(String source, String recordId, String sourceEventId, String message) {
        return new Relay_Error_Log__c(
            Source__c          = truncate(source, 80),
            Record_Id__c       = truncate(recordId, 18),
            Source_Event_Id__c = truncate(sourceEventId, 64),
            Message__c         = truncate(message, 32768));
    }

    public static void write(List<Relay_Error_Log__c> logs) {
        if (logs == null || logs.isEmpty()) {
            return;
        }
        // allOrNone=false: a log write must never fail the caller's transaction.
        Database.insert(logs, false);
    }

    private static String truncate(String value, Integer maxLength) {
        return value == null ? null : value.left(maxLength);
    }
}
