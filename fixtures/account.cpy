      * Synthetic account record: dynamic group is at the tail.
       01 ACCOUNT-RECORD.
          05 ACCOUNT-ID PIC X(8).
          05 LINE-COUNT PIC 9(2).
          05 BALANCE PIC S9(5)V9(2) COMP-3.
          05 DETAIL-LINES OCCURS 0 TO 3 TIMES
             DEPENDING ON LINE-COUNT.
             10 SKU PIC X(6).
             10 QUANTITY PIC 9(3).
             10 PRICE PIC S9(3)V9(2) COMP-3.
